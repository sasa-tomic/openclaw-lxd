#!/usr/bin/env python3
"""
One-time cleanup script to deduplicate existing Todoist tasks using LLM.

Strategy:
1. Fetch all tasks
2. Use LLM to identify duplicate groups
3. For each group:
   - Prefer tasks in projects over inbox
   - Keep the "best" task
   - Mark others as done
   - Optionally merge info from duplicates
"""

import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.todoist_client import TodoistClient
from lib.llm_utils import call_llm, extract_json


def get_project_name(task: dict, projects_by_id: dict) -> str:
    """Get project name for a task."""
    project_id = task.get("project_id")
    if project_id and project_id in projects_by_id:
        return projects_by_id[project_id].get("name", "Unknown")
    return "Inbox"


def score_task_for_keeping(task: dict, projects_by_id: dict) -> tuple:
    """Score a task for keeping (higher = better).

    Priority order:
    1. Has project (not inbox)
    2. Has due date
    3. Has labels
    4. Has description
    5. Higher priority (lower number in API)
    6. Newer creation date
    """
    has_project = 1 if task.get("project_id") else 0
    has_due = 1 if task.get("due") else 0
    has_labels = 1 if task.get("labels") else 0
    has_description = 1 if len(task.get("description", "")) > 0 else 0
    priority = 5 - task.get("priority", 4)

    created = task.get("created_at", "")

    return (has_project, has_due, has_labels, has_description, priority, created)


def find_candidate_duplicates(
    tasks: list[dict], threshold: float = 0.4
) -> list[list[int]]:
    """Quick text-based pre-filter to find candidate duplicates.

    Returns groups of task indices that have SOME similarity.
    """
    from difflib import SequenceMatcher

    candidates = []

    for i, task1 in enumerate(tasks):
        for j, task2 in enumerate(tasks[i + 1 :], start=i + 1):
            content1 = task1.get("content", "").split("[[")[0].strip().lower()
            content2 = task2.get("content", "").split("[[")[0].strip().lower()

            if len(content1) < 10 or len(content2) < 10:
                continue

            ratio = SequenceMatcher(None, content1, content2).ratio()

            if ratio >= threshold:
                candidates.append([i, j, ratio])

    groups = []
    used = set()

    for i, j, ratio in sorted(candidates, key=lambda x: x[2], reverse=True):
        if i in used or j in used:
            continue

        group = [i, j]
        used.add(i)
        used.add(j)

        for k in range(len(tasks)):
            if k in used:
                continue
            content_k = tasks[k].get("content", "").split("[[")[0].strip().lower()

            is_similar = False
            for idx in group:
                content_idx = (
                    tasks[idx].get("content", "").split("[[")[0].strip().lower()
                )
                if SequenceMatcher(None, content_k, content_idx).ratio() >= threshold:
                    is_similar = True
                    break

            if is_similar:
                group.append(k)
                used.add(k)

        if len(group) >= 2:
            groups.append(group)

    return groups


def identify_duplicate_groups(tasks: list[dict]) -> list[list[dict]]:
    """Use LLM to confirm which candidate groups are actual duplicates."""

    print(f"\n{'=' * 70}")
    print("ALL TASKS BEING ANALYZED:")
    print("=" * 70)
    for i, task in enumerate(tasks):
        content = task.get("content", "").split("[[")[0].strip()
        print(f"{i:3d}: {content}")
    print("=" * 70)

    print("\nFinding candidate duplicates (text similarity)...")
    candidate_groups = find_candidate_duplicates(tasks)

    if not candidate_groups:
        print("No candidate duplicates found")
        return []

    print(f"Found {len(candidate_groups)} candidate groups to verify with LLM")

    confirmed_groups = []

    for group_indices in candidate_groups:
        group_tasks = [tasks[i] for i in group_indices]

        tasks_summary = []
        for i, idx in enumerate(group_indices):
            content = tasks[idx].get("content", "").split("[[")[0].strip()
            tasks_summary.append({"idx": i, "original_idx": idx, "content": content})

        prompt = f"""DUPLICATE TASK VERIFICATION

You are verifying if these tasks are TRUE DUPLICATES or just similar.

CANDIDATE TASKS:
{json.dumps(tasks_summary, indent=2)}

INSTRUCTIONS:
1. Determine if these tasks represent the SAME underlying action/commitment
2. Be EXTREMELY OBJECTIVE and STRICT
3. Pay attention to ALL details: names, dates, specifics matter

DEFINITION OF DUPLICATE:
- Same core action targeting the SAME entity/person (e.g., "Call John" vs "Phone John about project")
- Same task with minor rewording (e.g., "Fix login bug" vs "Repair login bug")

NOT DUPLICATES (BE VERY CAREFUL):
- Actions involving DIFFERENT people/entities: "Call John" vs "Call Mary" = NOT DUPLICATE
- Different actions to same person: "Call John" vs "Email John" = NOT DUPLICATE
- Sequential or related tasks: "Draft report" vs "Review report" = NOT DUPLICATE
- Same general area but different specifics: "Fix login bug" vs "Fix signup bug" = NOT DUPLICATE
- Tasks mentioning different people/names = NEVER DUPLICATES

CRITICAL: If tasks mention different names, people, or entities, they are NEVER duplicates!

RESPONSE FORMAT (JSON):
{{
  "are_duplicates": true/false,
  "reasoning": "Brief explanation"
}}

Be EXTREMELY CONSERVATIVE. When in doubt, return false.

Output ONLY the JSON object."""

        success, response = call_llm(prompt, timeout=60)

        if not success:
            print(f"WARNING: LLM call failed for group, skipping: {response}")
            continue

        json_str = extract_json(response)
        if not json_str:
            print(f"WARNING: Failed to extract JSON, skipping group")
            continue

        try:
            result = json.loads(json_str)
            are_duplicates = result.get("are_duplicates", False)
            reasoning = result.get("reasoning", "")

            if are_duplicates:
                print(
                    f"✓ Confirmed duplicate group: {len(group_tasks)} tasks - {reasoning[:50]}"
                )
                confirmed_groups.append(group_tasks)
            else:
                print(f"✗ Not duplicates: {reasoning[:50]}")

        except json.JSONDecodeError as e:
            print(f"WARNING: Failed to parse JSON, skipping group: {e}")
            continue

    return confirmed_groups


def merge_task_info(
    keep_task: dict, duplicate_tasks: list[dict], projects_by_id: dict
) -> dict:
    """Merge information from duplicates into the task to keep."""

    updates = {}

    all_content = [keep_task.get("content", "")]
    for dup in duplicate_tasks:
        content = dup.get("content", "")
        if content and content not in all_content:
            all_content.append(content)

    if len(all_content) > 1:
        source_info = []
        for content in all_content[1:]:
            if "[[" in content:
                source_info.append(content)

        if source_info:
            current_content = keep_task.get("content", "")
            if "[[" not in current_content:
                for source in source_info:
                    if "[[" in source:
                        source_part = source[source.index("[[") :]
                        current_content += f" {source_part}"
                        break
            updates["content"] = current_content

    if not keep_task.get("due"):
        for dup in duplicate_tasks:
            if dup.get("due"):
                updates["due_string"] = dup["due"].get("date")
                break

    if not keep_task.get("labels"):
        for dup in duplicate_tasks:
            if dup.get("labels"):
                updates["labels"] = dup["labels"]
                break

    return updates


def preview_cleanup(duplicate_groups: list[list[dict]], projects_by_id: dict) -> bool:
    """Show preview of what will be done. Returns True if user confirms."""

    print("\n" + "=" * 70)
    print("DUPLICATE CLEANUP PREVIEW")
    print("=" * 70)

    total_duplicates = 0

    for i, group in enumerate(duplicate_groups, 1):
        print(f"\n--- Group {i} ({len(group)} tasks) ---")

        sorted_tasks = sorted(
            group, key=lambda t: score_task_for_keeping(t, projects_by_id), reverse=True
        )
        keep_task = sorted_tasks[0]
        mark_done = sorted_tasks[1:]

        keep_project = get_project_name(keep_task, projects_by_id)
        print(f"\n✅ KEEP: {keep_task['content'][:280]}")
        print(f"   Project: {keep_project}")
        print(f"   ID: {keep_task['id']}")

        print(f"\n❌ MARK DONE ({len(mark_done)} tasks):")
        for task in mark_done:
            project = get_project_name(task, projects_by_id)
            print(f"   • {task['content'][:280]} [{project}]")

        total_duplicates += len(mark_done)

    print("\n" + "=" * 70)
    print(f"SUMMARY:")
    print(f"  Duplicate groups: {len(duplicate_groups)}")
    print(f"  Tasks to mark done: {total_duplicates}")
    print(f"  Tasks to keep: {len(duplicate_groups)}")
    print("=" * 70)

    response = input("\nProceed with cleanup? [y/N]: ").strip().lower()
    return response == "y"


def run_cleanup(
    duplicate_groups: list[list[dict]], projects_by_id: dict, dry_run: bool = False
):
    """Execute the cleanup."""

    total_marked_done = 0
    total_updated = 0

    for i, group in enumerate(duplicate_groups, 1):
        sorted_tasks = sorted(
            group, key=lambda t: score_task_for_keeping(t, projects_by_id), reverse=True
        )
        keep_task = sorted_tasks[0]
        mark_done = sorted_tasks[1:]

        updates = merge_task_info(keep_task, mark_done, projects_by_id)

        if updates and not dry_run:
            if TodoistClient.update_task(keep_task["id"], **updates):
                print(f"✅ Updated kept task {keep_task['id']}")
                total_updated += 1

        for task in mark_done:
            if dry_run:
                print(f"[DRY-RUN] Would mark done: {task['id']}")
            else:
                if TodoistClient.complete_task(task["id"]):
                    print(f"❌ Marked done: {task['id']} - {task['content'][:40]}")
                    total_marked_done += 1

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Cleanup complete!")
    print(f"  Marked done: {total_marked_done}")
    print(f"  Updated: {total_updated}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Deduplicate Todoist tasks")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview only, don't make changes"
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    print("Fetching tasks from Todoist...")
    tasks = TodoistClient.get_tasks()

    if not tasks:
        print("No tasks found!")
        return 1

    print(f"Found {len(tasks)} tasks")

    print("\nFetching projects...")
    projects = TodoistClient.get_projects()
    projects_by_id = {p["id"]: p for p in projects}
    print(f"Found {len(projects)} projects")

    print("\nAnalyzing tasks for duplicates (using LLM)...")
    duplicate_groups = identify_duplicate_groups(tasks)

    if not duplicate_groups:
        print("\n✅ No duplicates found!")
        return 0

    print(f"\nFound {len(duplicate_groups)} duplicate groups")

    if args.dry_run:
        print("\n[DRY-RUN MODE - No changes will be made]")

    if not args.yes:
        if not preview_cleanup(duplicate_groups, projects_by_id):
            print("\nCancelled.")
            return 0

    run_cleanup(duplicate_groups, projects_by_id, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())

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


def identify_duplicate_groups(tasks: list[dict]) -> list[list[dict]]:
    """Use LLM to identify groups of duplicate tasks."""

    print(f"\n{'=' * 70}")
    print("ALL TASKS BEING ANALYZED:")
    print("=" * 70)
    for i, task in enumerate(tasks):
        content = task.get("content", "").split("[[")[0].strip()
        print(f"{i:3d}: {content}")
    print("=" * 70)

    tasks_summary = []
    for i, task in enumerate(tasks):
        content = task.get("content", "").split("[[")[0].strip()
        if len(content) > 3:
            tasks_summary.append({"idx": i, "content": content})

    if not tasks_summary:
        return []

    prompt = f"""Find duplicate tasks. Tasks with DIFFERENT names/people are NEVER duplicates.

TASKS:
{json.dumps(tasks_summary, indent=2)}

Return JSON: {{"groups": [[idx1, idx2], [idx3, idx4]]}}

Only include groups with 2+ tasks. Be conservative - when in doubt, don't group."""

    print("\nAnalyzing with LLM...")
    success, response = call_llm(prompt, timeout=180, json_mode=True)

    if not success:
        print(f"ERROR: LLM call failed: {response}")
        return []

    json_str = extract_json(response)
    if not json_str:
        print(f"ERROR: Failed to extract JSON from response")
        print(f"Response was:\n{response}")
        return []

    try:
        result = json.loads(json_str)

        # Handle both {"groups": [...]} and direct [...] formats
        if isinstance(result, list):
            groups = result
        elif isinstance(result, dict):
            groups = result.get("groups", [])
        else:
            print(f"ERROR: Unexpected JSON type: {type(result)}")
            return []

        duplicate_groups = []
        for group_indices in groups:
            if len(group_indices) >= 2:
                group_tasks = [tasks[i] for i in group_indices if i < len(tasks)]
                if len(group_tasks) >= 2:
                    duplicate_groups.append(group_tasks)

        return duplicate_groups

    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse JSON: {e}")
        return []


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


def interactive_cleanup(
    duplicate_groups: list[list[dict]], projects_by_id: dict, dry_run: bool = False
):
    """Interactive cleanup - ask for each duplicate group."""

    print("\n" + "=" * 70)
    print("INTERACTIVE DUPLICATE CLEANUP")
    print("=" * 70)

    total_marked_done = 0
    total_updated = 0

    for i, group in enumerate(duplicate_groups, 1):
        print(f"\n{'=' * 70}")
        print(f"GROUP {i}/{len(duplicate_groups)} - {len(group)} duplicate tasks")
        print("=" * 70)

        sorted_tasks = sorted(
            group, key=lambda t: score_task_for_keeping(t, projects_by_id), reverse=True
        )

        for j, task in enumerate(sorted_tasks, 1):
            project = get_project_name(task, projects_by_id)
            print(f"\n[{j}] {task['content'][:280]}")
            print(f"    Project: {project}")
            print(f"    ID: {task['id']}")

        print(f"\nOptions:")
        print(f"  1-{len(sorted_tasks)}  - Keep only this task, mark others done")
        print(f"  a     - Keep all (no changes) [default]")
        print(f"  s     - Skip this group")

        while True:
            try:
                response = input(f"\nYour choice [a]: ").strip().lower()

                if not response or response == "a":
                    print("Keeping all tasks in this group")
                    break
                elif response == "s":
                    print("Skipping this group")
                    break
                elif response.isdigit():
                    choice = int(response)
                    if 1 <= choice <= len(sorted_tasks):
                        keep_task = sorted_tasks[choice - 1]
                        mark_done = [
                            t for k, t in enumerate(sorted_tasks) if k != choice - 1
                        ]

                        print(
                            f"\n✅ Will keep: [{choice}] {keep_task['content'][:60]}..."
                        )
                        print(f"❌ Will mark done: {len(mark_done)} task(s)")

                        if not dry_run:
                            updates = merge_task_info(
                                keep_task, mark_done, projects_by_id
                            )

                            if updates:
                                if TodoistClient.update_task(
                                    keep_task["id"], **updates
                                ):
                                    print(f"   Updated kept task")
                                    total_updated += 1

                            for task in mark_done:
                                if TodoistClient.complete_task(task["id"]):
                                    print(f"   Marked done: {task['content'][:50]}...")
                                    total_marked_done += 1
                        else:
                            print(
                                f"   [DRY-RUN] Would mark done {len(mark_done)} task(s)"
                            )

                        break
                    else:
                        print(f"Please enter 1-{len(sorted_tasks)}, 'a', or 's'")
                else:
                    print(f"Please enter 1-{len(sorted_tasks)}, 'a', or 's'")
            except (ValueError, KeyboardInterrupt):
                print("\nKeeping all tasks in this group")
                break

    print("\n" + "=" * 70)
    print("CLEANUP COMPLETE")
    print("=" * 70)
    print(f"Groups processed: {len(duplicate_groups)}")
    if not dry_run:
        print(f"Tasks marked done: {total_marked_done}")
        print(f"Tasks updated: {total_updated}")
    else:
        print("[DRY-RUN MODE - No changes made]")
    print("=" * 70)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Deduplicate Todoist tasks")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview only, don't make changes"
    )
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

    interactive_cleanup(duplicate_groups, projects_by_id, dry_run=args.dry_run)

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

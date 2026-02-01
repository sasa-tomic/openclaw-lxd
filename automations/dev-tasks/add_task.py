#!/usr/bin/env python3
"""
Add Task - CLI to add tasks to the backlog.

Usage:
    add_task.py "Task title" --project decent-cloud --priority P1 --context "Details..."
    add_task.py "Task title" -p decent-cloud -P P2 -c "Details..."
"""

import argparse
import json
from task_manager import add_task, init_files


def main():
    parser = argparse.ArgumentParser(description="Add a task to the dev backlog")
    parser.add_argument("title", help="Task title")
    parser.add_argument("-p", "--project", required=True, 
                        help="Project name (decent-cloud, voki, axiom, other)")
    parser.add_argument("-P", "--priority", default="P2",
                        choices=["P0", "P1", "P2", "P3"],
                        help="Priority (P0=critical, P3=low)")
    parser.add_argument("-c", "--context", default="",
                        help="Additional context/description")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    
    args = parser.parse_args()
    
    # Ensure files exist
    init_files()
    
    task = add_task(
        title=args.title,
        project=args.project,
        priority=args.priority,
        context=args.context or f"Task: {args.title}",
    )
    
    if args.json:
        print(json.dumps({
            "id": task.id,
            "title": task.title,
            "project": task.project,
            "priority": task.priority,
        }))
    else:
        print(f"✅ Added task [{task.priority}] {task.title}")
        print(f"   ID: {task.id}")
        print(f"   Project: {task.project}")


if __name__ == "__main__":
    main()

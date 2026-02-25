#!/usr/bin/env python3
"""Clean up todoist:pending markers after tasks have been recovered."""

import re
from pathlib import Path

NOTES_PATH = Path("/projects/Notes")


def cleanup():
    pattern = re.compile(r"\s*❌\s*<!-- todoist:pending -->")
    count = 0

    for md_file in NOTES_PATH.rglob("*.md"):
        if any(skip in str(md_file) for skip in [".obsidian", ".trash", ".stversions"]):
            continue

        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
        except (IOError, OSError) as e:
            print(f"Warning: Could not read {md_file}: {e}")
            continue

        if "todoist:pending" not in content:
            continue

        new_content = pattern.sub("", content)
        if new_content != content:
            md_file.write_text(new_content, encoding="utf-8")
            count += 1
            print(f"Cleaned: {md_file.relative_to(NOTES_PATH)}")

    print(f"\nCleaned {count} files")


if __name__ == "__main__":
    cleanup()

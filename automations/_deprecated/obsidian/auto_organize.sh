#!/bin/bash
# Auto-organize Obsidian vault (safe, non-destructive moves)
#
# Rules:
# - Never delete notes.
# - Prefer obsidian-cli move (updates wikilinks).
# - Only act on clearly safe cases:
#   - root-level .md files that are not TODO.md / 🏠 Home.md
#   - daily notes YYYY-MM-DD.md (anywhere at root) -> Daily/<YYYY>/
# - Everything else: move to Archive/Unsorted/ (keeps info, easy to triage)
#
# Env:
#   NOTES_DIR (default /projects/Notes)
#   DRY_RUN=1 (print actions, don't move)

set -euo pipefail

NOTES_DIR="${NOTES_DIR:-/projects/Notes}"
DRY_RUN="${DRY_RUN:-0}"

have() { command -v "$1" >/dev/null 2>&1; }

if ! have obsidian-cli; then
  echo "obsidian-cli not found; cannot auto-organize" >&2
  exit 1
fi

move_note() {
  local src="$1" dst="$2"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY_RUN: obsidian-cli move \"$src\" \"$dst\""
  else
    obsidian-cli move "$src" "$dst"
    echo "moved: $src -> $dst"
  fi
}

# 1) Root-level cleanup
mapfile -t ROOT_FILES < <(find "$NOTES_DIR" -maxdepth 1 -type f -name "*.md" -printf "%f\n" | sort)

for f in "${ROOT_FILES[@]:-}"; do
  case "$f" in
    "TODO.md"|"🏠 Home.md")
      continue
      ;;
  esac

  # Daily note at root
  if [[ "$f" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\.md$ ]]; then
    year="${f:0:4}"
    move_note "$f" "Daily/$year/$f"
    continue
  fi

  # Otherwise: preserve info, but get it out of root
  month=$(date -u +%Y-%m)
  move_note "$f" "Archive/Unsorted/$month/$f"
done

# 2) (Optional future) misplaced daily notes in Daily/ without year folder could be handled here.

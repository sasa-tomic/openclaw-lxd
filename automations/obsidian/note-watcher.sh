#!/bin/bash
# Find Obsidian notes modified in the last N minutes
# Usage: note-watcher.sh [minutes] [vault_path]

MINUTES=${1:-60}
VAULT=${2:-/projects/Notes}

find "$VAULT" -type f -name "*.md" -mmin -"$MINUTES" 2>/dev/null | \
  grep -v '.obsidian' | \
  grep -v '.trash' | \
  sort

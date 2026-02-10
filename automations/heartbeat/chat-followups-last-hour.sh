#!/bin/bash
# Scan messenger logs (Signal/WhatsApp/Telegram) modified in the last N minutes
# and extract potential follow-ups / actions.
#
# NOTE: read-only. Does not modify chat logs.

set -euo pipefail

NOTES_DIR="${NOTES_DIR:-/projects/Notes}"
MINUTES="${MINUTES:-60}"
OUT_NOTE="${OUT_NOTE:-$NOTES_DIR/Pickle/chat-followups.md}"
OUT_DIR="${OUT_DIR:-$NOTES_DIR/Pickle/chat-followups}"
MAX_FILES="${MAX_FILES:-20}"
TAIL_LINES="${TAIL_LINES:-120}"

stamp() { date -u +"%Y-%m-%d %H:%M UTC"; }

# Find recently modified chat files
mapfile -t FILES < <(
  find "$NOTES_DIR" \( -path "$NOTES_DIR/Signal/*" -o -path "$NOTES_DIR/WhatsApp/*" -o -path "$NOTES_DIR/Telegram/*" \) \
    -type f -name "*.md" -mmin "-$MINUTES" 2>/dev/null | sort | head -n "$MAX_FILES"
)

if [ "${#FILES[@]}" -eq 0 ]; then
  exit 0
fi

# Heuristics: keep very conservative; just flag things for review.
PAT_ACTION='\b(i\x27ll|i will|we should|todo|follow up|remind|deadline|due|by (tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|call|meeting|send|pay)\b'
PAT_DECISION='\b(decided|let\x27s do|lets do|agreed|plan is|booked|scheduled|confirmed)\b'
PAT_CONFLICT='\b(conflict|contradict|actually|wait|no that\x27s wrong|correction)\b'

SECTION=""
SECTION+="\n\n## Chat follow-ups (last ${MINUTES}m) — $(stamp)\n"

found_any=0

for f in "${FILES[@]}"; do
  rel="${f#$NOTES_DIR/}"
  tail_content=$(tail -n "$TAIL_LINES" "$f" 2>/dev/null || true)
  if [ -z "$tail_content" ]; then
    continue
  fi

  # Extract matching lines (context-free but concise)
  hits=$(echo "$tail_content" | rg -i "$PAT_ACTION|$PAT_DECISION|$PAT_CONFLICT" -n || true)
  if [ -z "$hits" ]; then
    continue
  fi

  found_any=1
  SECTION+="\n- **${rel}**\n"
  # Keep only a few lines
  SECTION+=$(echo "$hits" | head -n 8 | sed 's/^/  - /')
  SECTION+="\n"
done

if [ "$found_any" -eq 0 ]; then
  exit 0
fi

mkdir -p "$(dirname "$OUT_NOTE")" "$OUT_DIR"

# Write this run to its own file (atomic), then add a link to the index note.
RUN_STAMP=$(date -u +"%Y-%m-%d_%H%M")
RUN_NOTE="$OUT_DIR/${RUN_STAMP}.md"
TMP_RUN="$RUN_NOTE.tmp"

{
  echo "# Chat follow-ups — $(stamp)"
  echo ""
  echo "(Auto-generated from last ${MINUTES} minutes of chat logs)"
  echo ""
  printf "%b" "$SECTION"
  echo ""
} > "$TMP_RUN"

mv "$TMP_RUN" "$RUN_NOTE"

# Maintain an index note that links to the per-run notes.
# Never truncate the index; only append a new link when we created a run note.
if [ ! -f "$OUT_NOTE" ]; then
  {
    echo "# Chat follow-ups (index)"
    echo ""
    echo "Auto-generated. Latest runs are appended below."
    echo ""
  } > "$OUT_NOTE"
fi

echo "- [[Pickle/chat-followups/${RUN_STAMP}|${RUN_STAMP}]]" >> "$OUT_NOTE"

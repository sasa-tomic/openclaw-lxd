#!/usr/bin/env python3
"""Chat Followups Scanner v2 - LLM-powered action extraction

Improved version with:
- LLM validation to reduce false positives
- More specific patterns (first-person commitments only)
- Filters out group chats without Mr. T's active participation
- Smarter context extraction

NOTE: read-only. Does not modify chat logs.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import NOTES_DIR as _NOTES_DIR, OPENCLAW_BIN

NOTES_DIR = Path(_NOTES_DIR)
MINUTES = int(os.environ.get("MINUTES", "60"))
OUT_DIR = Path(os.environ.get("OUT_DIR", str(NOTES_DIR / "Pickle/chat-followups")))
MAX_FILES = int(os.environ.get("MAX_FILES", "20"))
TAIL_LINES = int(os.environ.get("TAIL_LINES", "120"))

# Stricter patterns - focus on first-person commitments only
ACTION_PATTERNS = [
    r"\b(i'll|i will|i need to|i should|i have to|remind me|my task)\b",
    r"\b(i agreed to|i decided to|i booked|i scheduled|i confirmed)\b",
    r"\b(deadline|due date|by (tomorrow|next week|monday|tuesday|wednesday|thursday|friday))\b",
]

# Negative patterns - skip these
SKIP_PATTERNS = [
    r"^<!--",  # Skip HTML comments
    r"🔶|🟣|⬛️|🟡",  # Skip group chat badges (others talking)
    r"Matrix Telegram Bridge:",  # Skip bridge metadata
]

# Group chats to completely ignore (Mr. T not actively participating)
IGNORE_GROUPS = {
    "F-Droid Translators",
    "Weblate translators",
    # Add more here as needed
}


def is_group_chat_ignored(path: Path) -> bool:
    """Check if this is an ignored group chat."""
    for ignored in IGNORE_GROUPS:
        if ignored in str(path):
            return True
    return False


def find_recent_chat_files() -> list[Path]:
    """Find recently modified chat files."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MINUTES)
    files = []

    for chat_type in ["Signal/DMs", "WhatsApp", "Telegram/DMs"]:
        chat_dir = NOTES_DIR / chat_type
        if not chat_dir.exists():
            continue

        for path in chat_dir.rglob("*.md"):
            # Skip Reference/ and _reports/ subdirectories entirely
            if "Reference" in path.parts or "_reports" in path.parts:
                continue
                
            if is_group_chat_ignored(path):
                continue

            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if mtime > cutoff:
                    files.append(path)
            except Exception:
                continue

    return sorted(files)[:MAX_FILES]


def extract_messages(content: str) -> list[dict]:
    """Extract structured messages from chat log."""
    lines = content.strip().split("\n")[-TAIL_LINES:]
    messages = []

    for line in lines:
        # Skip if matches any skip pattern
        if any(re.search(pat, line, re.IGNORECASE) for pat in SKIP_PATTERNS):
            continue

        # Try multiple formats:
        # Format 1: [2026-02-23 08:42:11] Sender: message text
        # Format 2: [2026-02-23 08:42:11] **Sender**: message text
        # Format 3: [2026-02-23 08:42:11] message text (no sender)
        
        match = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(?:(?:\*\*)?([^*:\n]+?)(?:\*\*)?:)?\s*(.+)", line)
        if match:
            timestamp, sender, text = match.groups()
            # Clean up sender (remove ** if present)
            if sender:
                sender = sender.strip().replace("**", "")
            messages.append({
                "timestamp": timestamp,
                "sender": sender or "Unknown",
                "text": text.strip(),
            })

    return messages


def has_action_indicators(messages: list[dict]) -> bool:
    """Quick regex check - does this conversation have potential actions?"""
    combined_text = " ".join(m["text"] for m in messages)
    combined_pattern = "|".join(f"({p})" for p in ACTION_PATTERNS)
    return bool(re.search(combined_pattern, combined_text, re.IGNORECASE))


def analyze_with_llm(chat_name: str, messages: list[dict]) -> dict | None:
    """Use LLM to extract actual action items from conversation."""
    if not messages:
        return None

    # Build context
    conversation = "\n".join(
        f"[{m['timestamp']}] {m['sender']}: {m['text']}"
        for m in messages[-20:]  # Last 20 messages for context
    )

    prompt = f"""Analyze this chat conversation and extract ONLY actionable items for Mr. T (Saša).

Chat: {chat_name}

Recent messages:
{conversation}

RULES:
1. ONLY extract if Mr. T explicitly committed to doing something ("I'll do X", "I need to Y", "Remind me to Z")
2. SKIP if:
   - It's just casual conversation
   - Someone else is committing to something
   - It's a question without commitment
   - It's general discussion
3. Extract:
   - What needs to be done
   - By when (if mentioned)
   - Any relevant context

Output JSON:
{{
  "has_action": true/false,
  "items": [
    {{"task": "...", "deadline": "...", "context": "..."}}
  ],
  "reasoning": "why this is/isn't actionable"
}}

If there's NO clear action for Mr. T, return {{"has_action": false, "items": [], "reasoning": "..."}}.
"""

    try:
        result = subprocess.run(
            [
                OPENCLAW_BIN,
                "agent",
                "--agent", "main",
                "--local",
                "--message", prompt,
                "--timeout", "60",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=70,
        )

        if result.returncode != 0:
            return None

        payload = json.loads(result.stdout)
        payloads = payload.get("payloads") or payload.get("result", {}).get("payloads", [])
        if not payloads:
            return None

        text = "\n".join(p.get("text", "") for p in payloads if p.get("text"))

        # Try to extract JSON from response
        json_match = re.search(r'\{[\s\S]*"has_action"[\s\S]*\}', text)
        if json_match:
            return json.loads(json_match.group(0))

        return None

    except Exception as e:
        print(f"LLM analysis failed: {e}", file=sys.stderr)
        return None


def main():
    files = find_recent_chat_files()

    if not files:
        print("No recent chat files found")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    actionable_items = []

    for path in files:
        rel_path = str(path.relative_to(NOTES_DIR))
        print(f"Scanning: {rel_path}", flush=True)

        try:
            content = path.read_text()
        except Exception as e:
            print(f"  ⚠️ Failed to read: {e}", file=sys.stderr)
            continue

        messages = extract_messages(content)
        if not messages:
            print(f"  → No messages extracted", flush=True)
            continue

        # Quick regex filter before expensive LLM call
        if not has_action_indicators(messages):
            print(f"  → No action indicators", flush=True)
            continue

        # LLM validation
        print(f"  → Potential action, analyzing with LLM...", flush=True)
        analysis = analyze_with_llm(rel_path, messages)

        if analysis and analysis.get("has_action"):
            actionable_items.append({
                "chat": rel_path,
                "analysis": analysis,
            })
            print(f"  ✓ Action found: {len(analysis.get('items', []))} item(s)", flush=True)
        else:
            reason = analysis.get("reasoning", "unknown") if analysis else "LLM call failed"
            print(f"  → No action: {reason}", flush=True)

    if not actionable_items:
        print("\n✓ No actionable items found")
        return 0

    # Write report
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    run_note = OUT_DIR / f"{run_stamp}.md"

    sections = []
    for item in actionable_items:
        chat = item["chat"]
        analysis = item["analysis"]

        section = f"\n## {chat}\n\n"
        section += f"**Reasoning:** {analysis.get('reasoning', 'N/A')}\n\n"

        for action in analysis.get("items", []):
            task = action.get("task", "N/A")
            deadline = action.get("deadline", "No deadline")
            context = action.get("context", "")

            section += f"- [ ] **{task}**\n"
            if deadline != "No deadline":
                section += f"  - Deadline: {deadline}\n"
            if context:
                section += f"  - Context: {context}\n"

        sections.append(section)

    content = f"""# Chat follow-ups — {stamp}

(Auto-generated from last {MINUTES} minutes, LLM-validated)

{"".join(sections)}
"""

    # Atomic write
    temp = run_note.with_suffix(".tmp")
    temp.write_text(content)
    temp.replace(run_note)

    print(f"\n✓ Report saved: {run_note}")
    print(f"  Found {len(actionable_items)} actionable chat(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""LLM-assisted Obsidian organizer (suggestions only).

Creates/updates a queue note with suggested organization actions.

Why:
- Rule-based cleanup isn't very helpful when the problem is semantic (where does this note belong?).

Safety:
- Suggestion-only. Does NOT move/rename/delete files.
- Skips messenger sync folders and Daily notes by default.

Output:
- /projects/Notes/.organization/llm-organization-queue.md

Usage:
- python3 llm_organizer_suggest.py [--limit 30] [--days 14]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOTES_DIR = Path("/projects/Notes")
# Obsidian doesn't show dotfolders by default, so keep the queue/report visible.
ORG_QUEUE = NOTES_DIR / "Pickle" / "organization-report.md"

SKIP_DIRS = {
    "WhatsApp",
    "Signal",
    "Telegram",
    "Daily",
    ".obsidian",
    ".trash",
    ".stfolder",
    ".organization",
}


@dataclass
class Candidate:
    relpath: str
    mtime: str
    excerpt: str


def _iter_candidates(days: int, limit: int) -> list[Candidate]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[tuple[float, Candidate]] = []

    for path in NOTES_DIR.rglob("*.md"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            st = path.stat()
        except OSError:
            continue

        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            continue

        # skip very small files (often indices), but keep if at root
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        lines = [ln.rstrip() for ln in text.splitlines()]
        excerpt_lines = []
        for ln in lines[:60]:
            if ln.strip() == "":
                excerpt_lines.append("")
                continue
            excerpt_lines.append(ln)
        excerpt = "\n".join(excerpt_lines).strip()

        cand = Candidate(
            relpath=str(path.relative_to(NOTES_DIR)),
            mtime=mtime.isoformat(),
            excerpt=excerpt[:2000],
        )
        out.append((st.st_mtime, cand))

    out.sort(key=lambda t: t[0], reverse=True)
    return [c for _, c in out[:limit]]


def _run_agent(candidates: list[Candidate]) -> list[dict]:
    payload = {
        "candidates": [c.__dict__ for c in candidates],
        "folderStructure": {
            "Projects": ["AxiomLabs", "Decent Cloud", "VoKI", "Ideas"],
            "People": True,
            "Reference": True,
            "Travel": True,
            "Interviews": True,
            "Archive": True,
        },
        "rules": [
            "Suggestion-only. Do not propose deleting content.",
            "Prefer moves into Projects/*, People, Reference, Travel, Interviews, Archive.",
            "If unsure, propose leaving in place but add links or a short 'See also' section.",
            "Avoid churn: don't propose renames unless clearly beneficial.",
        ],
        "outputFormat": {
            "type": "json",
            "schema": [
                {
                    "relpath": "<existing file relpath>",
                    "suggestedAction": "move|rename|link|split|none",
                    "targetPath": "<new relpath if move/rename>",
                    "rationale": "<why>",
                    "confidence": 0.0,
                    "extra": {"links": ["[[Note]]", "..."], "tags": ["tag"], "notes": "..."},
                }
            ],
        },
    }

    msg = (
        "You are an Obsidian vault organizer.\n"
        "Given the folder structure and a list of recently modified markdown notes (with excerpts), "
        "suggest organization actions.\n\n"
        "Return ONLY valid JSON (an array of objects) exactly matching the schema described in payload.outputFormat.schema.\n\n"
        f"INPUT_JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    r = subprocess.run(
        [
            "openclaw",
            "agent",
            "--agent",
            "main",
            "--message",
            msg,
            "--timeout",
            "240",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=260,
    )
    if r.returncode != 0:
        raise RuntimeError(f"agent failed: {r.returncode}\n{r.stdout[:800]}\n{r.stderr[:800]}")

    outer = json.loads(r.stdout)
    text = "\n".join(p.get("text", "") for p in outer.get("result", {}).get("payloads", []))
    # model must output raw JSON array
    return json.loads(text)


def _write_queue(items: list[dict], candidates: list[Candidate]) -> None:
    ORG_QUEUE.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = [
        "# Organization Report (LLM)",
        "",
        f"Last run: {now}",
        "",
        "This is suggestion-only. Apply moves manually (or ask Pickle to apply).",
        "",
        "## Suggestions",
        "",
    ]

    # build quick map for mtime
    mt = {c.relpath: c.mtime for c in candidates}

    lines = header
    for it in items:
        rel = it.get("relpath", "")
        act = it.get("suggestedAction", "")
        tgt = it.get("targetPath", "")
        conf = it.get("confidence", "")
        rationale = it.get("rationale", "").strip()

        lines.append(f"- [ ] `{act}` `{rel}`" + (f" → `{tgt}`" if tgt else ""))
        if rel in mt:
            lines.append(f"  - mtime: {mt[rel]}")
        if rationale:
            lines.append(f"  - why: {rationale}")
        if conf != "":
            lines.append(f"  - confidence: {conf}")
        extra = it.get("extra") or {}
        links = extra.get("links") or []
        if links:
            lines.append(f"  - links: {', '.join(links)}")
        tags = extra.get("tags") or []
        if tags:
            lines.append(f"  - tags: {', '.join(tags)}")
        notes = (extra.get("notes") or "").strip()
        if notes:
            lines.append(f"  - notes: {notes}")

    ORG_QUEUE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    candidates = _iter_candidates(days=args.days, limit=args.limit)
    if not candidates:
        ORG_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        ORG_QUEUE.write_text(
            f"# LLM Organization Queue\n\nLast run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\nNo candidates found.\n",
            encoding="utf-8",
        )
        return 0

    suggestions = _run_agent(candidates)
    if not isinstance(suggestions, list):
        raise RuntimeError("agent did not return a JSON array")

    _write_queue(suggestions, candidates)
    print(f"Wrote {len(suggestions)} suggestions to {ORG_QUEUE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

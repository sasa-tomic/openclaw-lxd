#!/usr/bin/env python3
"""Obsidian helper: add wikilinks + report possible conflicts.

Design goals:
- Safe: only *adds* links, never deletes content.
- Conservative: only link exact note titles found in plain text.
- Report-only for conflicts (no automatic edits).

Default scope:
- Notes modified in last N days (default 2)
- Skips chat/log folders and hidden folders.

Outputs JSON to stdout:
{
  "linked": [{"file": "...", "added": 3}],
  "skipped": [...],
  "conflicts": [{"term": "...", "dates": [...], "hits": [...] }]
}
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKIP_DIRS = {
    ".obsidian",
    ".trash",
    ".organization",
    ".stfolder",
    # chat logs
    "Signal",
    "WhatsApp",
    "Telegram",
    # assistant/system meta notes (keep stable)
    "Pickle",
    # bulk archives
    "Archive",
    # reference materials (no TODOs)
    "Reference",
    # auto-generated reports (no TODOs)
    "_reports",
}

DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")


def is_skipped(path: Path) -> bool:
    parts = set(path.parts)
    return any(p in SKIP_DIRS for p in parts) or any(p.startswith(".") for p in path.parts)


def note_title_from_path(p: Path) -> str:
    return p.stem


def load_titles(vault: Path) -> set[str]:
    titles: set[str] = set()
    for p in vault.rglob("*.md"):
        if is_skipped(p):
            continue
        t = note_title_from_path(p)
        # avoid super-short / noisy titles
        if len(t) < 3:
            continue
        titles.add(t)
    return titles


def iter_recent_notes(vault: Path, days: int) -> list[Path]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[Path] = []
    for p in vault.rglob("*.md"):
        if is_skipped(p):
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except FileNotFoundError:
            continue
        if mtime >= cutoff:
            out.append(p)
    return out


def add_links(text: str, titles: list[str], max_links_per_file: int) -> tuple[str, int]:
    # Avoid linking inside existing wikilinks by temporarily masking them.
    masks: list[str] = []

    def _mask(m: re.Match) -> str:
        masks.append(m.group(0))
        return f"@@WIKILINK_{len(masks)-1}@@"

    masked = WIKILINK_RE.sub(_mask, text)

    added = 0
    for title in titles:
        if added >= max_links_per_file:
            break
        # Whole-word-ish match, but allow spaces and punctuation around.
        pat = re.compile(rf"(?<!\[)\b{re.escape(title)}\b")
        if pat.search(masked):
            masked, n = pat.subn(f"[[{title}]]", masked, count=1)
            if n:
                added += 1

    # Unmask
    for i, orig in enumerate(masks):
        masked = masked.replace(f"@@WIKILINK_{i}@@", orig)

    return masked, added


def conflict_scan(vault: Path, terms: list[str], days: int) -> list[dict]:
    # Heuristic: if a term appears with multiple different YYYY-MM-DD dates across notes,
    # flag it for review.
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    term_dates: dict[str, set[str]] = defaultdict(set)
    term_hits: dict[str, list[dict]] = defaultdict(list)

    for p in vault.rglob("*.md"):
        if is_skipped(p):
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except FileNotFoundError:
            continue
        if mtime < cutoff:
            continue

        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        lower = content.lower()
        for term in terms:
            if term.lower() not in lower:
                continue
            for m in DATE_RE.finditer(content):
                d = m.group(1)
                # only count a date if it's on a line where the term appears
                line_start = content.rfind("\n", 0, m.start()) + 1
                line_end = content.find("\n", m.end())
                if line_end == -1:
                    line_end = len(content)
                line = content[line_start:line_end]
                if term.lower() in line.lower():
                    term_dates[term].add(d)
                    term_hits[term].append({"file": str(p.relative_to(vault)), "line": line.strip()})

    conflicts = []
    for term, dates in term_dates.items():
        if len(dates) >= 2:
            conflicts.append({
                "term": term,
                "dates": sorted(dates),
                "hits": term_hits[term][:10],
            })
    return conflicts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default="/projects/Notes")
    ap.add_argument("--days", type=int, default=int(os.environ.get("OBSIDIAN_LINK_DAYS", "2")))
    ap.add_argument("--max-links-per-file", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--conflict-terms", nargs="*", default=[
        "Ski trip",
        "Stripe",
        "Golem",
        "VoKI",
        "Decent Cloud",
        "Axiom",
    ])
    args = ap.parse_args()

    vault = Path(args.vault)
    titles = sorted(load_titles(vault), key=lambda s: (-len(s), s.lower()))

    recent = iter_recent_notes(vault, args.days)

    linked = []
    skipped = []

    for p in recent:
        rel = str(p.relative_to(vault))
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            skipped.append({"file": rel, "reason": f"read error: {e}"})
            continue

        new_text, added = add_links(text, titles=titles, max_links_per_file=args.max_links_per_file)
        if added and (new_text != text):
            linked.append({"file": rel, "added": added})
            if not args.dry_run:
                p.write_text(new_text, encoding="utf-8")

    conflicts = conflict_scan(vault, terms=args.conflict_terms, days=max(args.days, 14))

    print(json.dumps({"linked": linked, "skipped": skipped, "conflicts": conflicts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

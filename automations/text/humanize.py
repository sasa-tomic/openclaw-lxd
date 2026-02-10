#!/usr/bin/env python3
"""Lightweight, deterministic 'humanize' pass.

Goal: reduce common AI-writing tells without changing meaning too much.
Reads stdin, writes stdout.

This is NOT a model. It's a simple hygiene filter to add resilience.
"""

from __future__ import annotations

import re
import sys

TEXT = sys.stdin.read()

# Normalize quotes/dashes to avoid curly-quote / em-dash tells.
TEXT = (
    TEXT.replace("“", '"')
    .replace("”", '"')
    .replace("’", "'")
    .replace("‘", "'")
    .replace("–", "-")
    .replace("—", " - ")
)

# Kill classic chatbot endings.
CHATBOT_ARTIFACTS = [
    r"\bI hope this helps\.?\b",
    r"\bLet me know if you (have|need) (anything else|more)\.?\b",
    r"\bI'd be happy to help\.?\b",
]
for pat in CHATBOT_ARTIFACTS:
    TEXT = re.sub(pat, "", TEXT, flags=re.IGNORECASE)

# Trim AI-ish openers.
TEXT = re.sub(r"^(Certainly|Sure|Of course)[:,]?\s+", "", TEXT.strip(), flags=re.IGNORECASE)

# Replace some high-frequency AI vocabulary with simpler words.
REPLACEMENTS = {
    "Additionally": "Also",
    "Furthermore": "Also",
    "However": "But",
    "Therefore": "So",
    "utilize": "use",
    "leverage": "use",
    "delve": "dig",
    "underscore": "show",
    "showcase": "show",
    "foster": "help",
    "garner": "get",
    "enhance": "improve",
    "crucial": "important",
    "pivotal": "key",
}

for k, v in REPLACEMENTS.items():
    TEXT = re.sub(rf"\b{k}\b", v, TEXT)
    TEXT = re.sub(rf"\b{k.lower()}\b", v.lower(), TEXT)

# Remove some filler phrases.
FILLER = [
    (r"\bin order to\b", "to"),
    (r"\bdue to the fact that\b", "because"),
    (r"\bit is important to note that\b", ""),
]
for pat, repl in FILLER:
    TEXT = re.sub(pat, repl, TEXT, flags=re.IGNORECASE)

# Clean up orphan punctuation / empty lines created by removals.
TEXT = re.sub(r"^\s*[-–—,:;.!?]+\s*$", "", TEXT, flags=re.MULTILINE)
# Also remove lines (or leading fragments) that became just a dash + transition word.
TEXT = re.sub(r"^\s*-\s*(also|but|so)[, ]*!?\s*$", "", TEXT, flags=re.MULTILINE | re.IGNORECASE)
TEXT = re.sub(r"^\s*-\s*(also|but|so)[, ]*!?\s+", "", TEXT, flags=re.IGNORECASE)

# Collapse whitespace.
TEXT = re.sub(r"[ \t]+", " ", TEXT)
TEXT = re.sub(r"\n{3,}", "\n\n", TEXT)
TEXT = TEXT.strip() + "\n"

sys.stdout.write(TEXT)

#!/usr/bin/env python3
"""Shared utilities for contact name validation."""


def is_valid_contact_name(name: str) -> bool:
    """Check if a contact name is valid for processing.

    Filters out:
    - Phone numbers (starting with +)
    - WhatsApp JIDs (containing @ and whatsapp.net)
    - Numeric-only names
    - Placeholder names like ".", "Unknown"
    - Very short names (< 2 characters)
    """
    if not name:
        return False
    if name.startswith("+"):
        return False
    if "@" in name and "whatsapp.net" in name:
        return False
    if name.replace(" ", "").isdigit():
        return False
    if name in [".", "Unknown", "unknown"]:
        return False
    if len(name) < 2:
        return False
    return True

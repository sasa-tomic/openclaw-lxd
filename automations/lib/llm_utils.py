#!/usr/bin/env python3
"""Unified LLM utility module.

Provides a consistent interface for LLM calls across the codebase with:
- Direct API support (OpenAI-compatible)
- Retry logic with exponential backoff on rate limits
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

try:
    from dotenv import load_dotenv

    load_dotenv(Path.home() / ".openclaw" / ".env")
except ImportError:
    pass

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")

_RETRY_DELAYS = [30, 60, 120]


def validate_llm_config() -> None:
    """Validate LLM configuration. Call at startup if LLM is needed."""
    if not OPENAI_API_KEY:
        raise ValueError(
            "OPENAI_API_KEY not set. Set it in ~/.openclaw/.env or environment variable"
        )
    if not OPENAI_BASE_URL:
        raise ValueError(
            "OPENAI_BASE_URL not set. "
            "Set it in ~/.openclaw/.env or environment variable"
        )
    if not OPENAI_BASE_URL:
        raise ValueError(
            "OPENAI_BASE_URL not set. "
            "Set it in ~/.openclaw/.env or environment variable"
        )


def extract_json(text: str) -> str | None:
    """Extract JSON object or array from text, handling common LLM output issues."""
    if not text:
        return None

    text = text.strip()

    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, ValueError):
        pass

    for open_ch, close_ch in [("{", "}"), ("[", "]")]:
        depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == open_ch:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start : i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except (json.JSONDecodeError, ValueError):
                        fixed = re.sub(r",\s*}", "}", candidate)
                        fixed = re.sub(r",\s*]", "]", fixed)
                        try:
                            json.loads(fixed)
                            return fixed
                        except (json.JSONDecodeError, ValueError):
                            start = None
                            continue

    return None


def call_llm(
    prompt: str,
    max_retries: int = 4,
    timeout: int = 120,
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
    json_mode: bool = False,
) -> tuple[bool, str]:
    """Call LLM with prompt, return (success, response).

    Args:
        prompt: The text prompt to send to the LLM
        max_retries: Maximum retry attempts per model (default 4)
        timeout: Request timeout in seconds (default 120)
        model: Override primary model (uses OPENAI_MODEL env var if not set)
        fallback_model: Optional fallback model if primary fails
        json_mode: Force JSON response format (default False)

    Returns:
        Tuple of (success: bool, response: str)
        - On success: (True, response_text)
        - On failure: (False, error_message)
    """
    api_key = OPENAI_API_KEY
    base_url = OPENAI_BASE_URL
    primary_model = model or os.environ.get("OPENAI_MODEL", "GLM-5")
    fb_model = fallback_model or os.environ.get("OPENAI_FALLBACK_MODEL", "")

    if not (api_key and base_url):
        return (
            False,
            "OPENAI_API_KEY and OPENAI_BASE_URL must be set in ~/.openclaw/.env or environment",
        )

    models_to_try: list[tuple[str, str]] = [(primary_model, "primary")]
    if fb_model and fb_model != primary_model:
        models_to_try.append((fb_model, "fallback"))

    last_error = "Unknown error"

    for model_name, model_label in models_to_try:
        for attempt in range(max_retries):
            attempt_num = attempt + 1
            print(
                f"LLM: calling {model_label} model '{model_name}' "
                f"(attempt {attempt_num}/{max_retries})",
                flush=True,
            )

            try:
                response = requests.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7,
                        **(
                            {"response_format": {"type": "json_object"}}
                            if json_mode
                            else {}
                        ),
                    },
                    timeout=timeout,
                )
            except requests.Timeout:
                last_error = (
                    f"{model_label} model '{model_name}' timed out after {timeout}s"
                )
                print(f"LLM: {last_error}", file=sys.stderr)
                if attempt < max_retries - 1:
                    time.sleep(10)
                    continue
                break
            except Exception as exc:
                last_error = f"{model_label} model '{model_name}' request failed: {exc}"
                print(f"LLM: {last_error}", file=sys.stderr)
                break

            if response.status_code == 200:
                content = _extract_response_content(response)
                if content:
                    print(
                        f"LLM: success with {model_label} model '{model_name}'",
                        flush=True,
                    )
                    return (True, content)
                else:
                    last_error = (
                        f"{model_label} model '{model_name}' returned empty content"
                        f" — body: {response.text[:500]}"
                    )
                    print(f"LLM: {last_error}", file=sys.stderr)
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    break

            elif response.status_code == 429:
                if attempt < max_retries - 1 and attempt < len(_RETRY_DELAYS):
                    wait = _RETRY_DELAYS[attempt]
                    print(
                        f"LLM: HTTP 429 rate limit on {model_label} model '{model_name}' "
                        f"— body: {response.text[:500]}. Waiting {wait}s before retry...",
                        flush=True,
                    )
                    time.sleep(wait)
                    continue
                else:
                    last_error = (
                        f"HTTP 429 rate limit on {model_label} model '{model_name}' - all attempts exhausted"
                        f" — body: {response.text[:500]}"
                    )
                    print(f"LLM: {last_error}", file=sys.stderr)
                    break

            else:
                last_error = f"{model_label} model '{model_name}' returned HTTP {response.status_code} — body: {response.text[:500]}"
                print(f"LLM: {last_error}", file=sys.stderr)
                break

    return (False, last_error)


def _extract_response_content(response: requests.Response) -> str | None:
    """Extract content from LLM API response."""
    try:
        data = response.json()
        message = data.get("choices", [{}])[0].get("message", {})
        content = message.get("content")

        if not content:
            reasoning = message.get("reasoning_content", "")
            if reasoning:
                json_candidate = extract_json(reasoning)
                content = json_candidate if json_candidate else reasoning.strip()

        return content.strip() if content else None
    except Exception:
        return None


def call_llm_simple(
    prompt: str, timeout: int = 120, json_mode: bool = False
) -> str | None:
    """Simple LLM call returning just the response string (for backward compatibility).

    Deprecated: Use call_llm() instead for better error handling.

    Args:
        prompt: The text prompt to send to the LLM
        timeout: Request timeout in seconds (default 120)

    Returns:
        Response text on success, None on failure
    """
    success, response = call_llm(
        prompt, max_retries=4, timeout=timeout, json_mode=json_mode
    )
    return response if success else None

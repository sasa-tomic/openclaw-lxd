#!/usr/bin/env python3
"""Unified LLM utility module.

Provides a consistent interface for LLM calls across the codebase with:
- Direct API support (OpenAI-compatible)
- opencode CLI fallback
- Retry logic with exponential backoff on rate limits
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Optional

import requests

from lib.config import OPENCODE_BIN

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")

_RETRY_DELAYS = [30, 60, 120]


def validate_llm_config() -> None:
    """Validate LLM configuration. Call at startup if LLM is needed."""
    if not OPENAI_API_KEY:
        raise ValueError(
            "OPENAI_API_KEY environment variable not set. "
            "Set it with: export OPENAI_API_KEY='your-key'"
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
    max_retries: int = 3,
    timeout: int = 120,
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
) -> tuple[bool, str]:
    """Call LLM with prompt, return (success, response).

    Args:
        prompt: The text prompt to send to the LLM
        max_retries: Maximum retry attempts per model (default 3)
        timeout: Request timeout in seconds (default 120)
        model: Override primary model (uses OPENAI_MODEL env var if not set)
        fallback_model: Optional fallback model if primary fails

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
        print(
            "LLM: No direct API credentials; using opencode CLI fallback",
            file=sys.stderr,
            flush=True,
        )
        return _call_llm_via_opencode(prompt, timeout)

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
                file=sys.stderr,
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
                        "max_tokens": 2000,
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
                        file=sys.stderr,
                        flush=True,
                    )
                    return (True, content)
                else:
                    last_error = (
                        f"{model_label} model '{model_name}' returned empty content"
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
                        f"LLM: HTTP 429 rate limit on {model_label} model '{model_name}'. "
                        f"Waiting {wait}s before retry...",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(wait)
                    continue
                else:
                    last_error = f"HTTP 429 rate limit on {model_label} model '{model_name}' - all attempts exhausted"
                    print(f"LLM: {last_error}", file=sys.stderr)
                    break

            else:
                last_error = f"{model_label} model '{model_name}' returned HTTP {response.status_code}: {response.text[:300]}"
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


def _call_llm_via_opencode(prompt: str, timeout: int) -> tuple[bool, str]:
    """Fallback LLM call via opencode CLI."""
    if not os.path.exists(OPENCODE_BIN):
        return (False, f"opencode not found at {OPENCODE_BIN}")

    try:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"

        result = subprocess.run(
            [OPENCODE_BIN, "run", prompt],
            capture_output=True,
            text=True,
            timeout=timeout + 20,
            env=env,
        )

        if result.returncode != 0:
            error_msg = (
                result.stderr.strip()[:300] if result.stderr else "Unknown error"
            )
            return (False, f"opencode failed (exit {result.returncode}): {error_msg}")

        output = result.stdout.strip()
        if not output:
            return (False, "opencode returned empty response")

        return (True, output)

    except subprocess.TimeoutExpired:
        return (False, f"opencode timed out after {timeout + 20}s")
    except Exception as e:
        return (False, f"opencode call failed: {e}")


def call_llm_simple(prompt: str, timeout: int = 120) -> str | None:
    """Simple LLM call returning just the response string (for backward compatibility).

    Deprecated: Use call_llm() instead for better error handling.

    Args:
        prompt: The text prompt to send to the LLM
        timeout: Request timeout in seconds (default 120)

    Returns:
        Response text on success, None on failure
    """
    success, response = call_llm(prompt, max_retries=1, timeout=timeout)
    return response if success else None

#!/usr/bin/env python3
"""Automatic service repair script for Twitter automation.

Invoked by Prefect failure/crash hooks. Acquires a lock, checks cooldown,
creates a git worktree, runs opencode to fix the failing flow, runs tests,
and merges if tests pass.

Usage:
    repair_service.py --flow-name twitter-engagement \
                      --flow-run-id <uuid> \
                      --state-message "RuntimeError: ..." \
                      [--log-snippet "..."]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Allow importing from lib/
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import OPENCODE_BIN
from lib.state_utils import load_state, save_state
from lib.telegram_utils import send_telegram

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOCK_FILE = "/tmp/twitter-repair.lock"
STATE_FILE = Path("/home/openclaw/clawd/memory/twitter-repair-state.json")
LOG_DIR = Path("/home/openclaw/clawd/logs")
REPAIR_COOLDOWN_HOURS = 2
MAX_HISTORY = 100
MAX_REPAIR_ATTEMPTS = 3

AUTOMATIONS_DIR = Path("/projects/automations")
WORKTREES_DIR = AUTOMATIONS_DIR / ".claude" / "worktrees"

_STATE_DEFAULT: dict = {"lastRepairs": {}, "repairHistory": []}

# ---------------------------------------------------------------------------
# Flow → script mapping
# ---------------------------------------------------------------------------

FLOW_TO_SCRIPT: dict[str, str] = {
    "twitter-engagement": "twitter_engagement.py",
    "twitter-original-content": "post_original_content.py",
    "twitter-weekly-thread": "post_thread.py",
    "twitter-daily-eval": "daily_strategy_eval.py",
    "twitter-morning-research": "twitter_morning.py",
    "twitter-cdp-health": "cdp_health_check.py",
    "twitter-reply-monitor": "reply_monitor.py",
    "twitter-target-monitor": "target_monitor.py",
    "twitter-timeline-monitor": "timeline_monitor.py",
    "twitter-account-discovery": "account_discovery.py",
    "twitter-search-queue": "search_queue.py",
}

SHARED_MODULES: list[str] = ["twitter_utils.py", "cdp.py", "db.py"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _related_scripts(flow_name: str, log_snippet: str) -> list[str]:
    """Return list of scripts that share a module mentioned in the log."""
    all_scripts = list(FLOW_TO_SCRIPT.values())
    primary = FLOW_TO_SCRIPT.get(flow_name, "")
    for shared in SHARED_MODULES:
        if shared in log_snippet:
            return [s for s in all_scripts if s != primary]
    return []


def _is_signal_exit(state_message: str) -> bool:
    """Return True if the failure is due to a signal, not a code bug."""
    lower = state_message.lower()
    return any(kw in lower for kw in ("interrupt signal", "sigkill", "sigterm"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_cooldown(state: dict, flow_name: str) -> bool:
    """Return True if we are still within the cooldown window."""
    last_str = state.get("lastRepairs", {}).get(flow_name)
    if not last_str:
        return False
    try:
        last_dt = datetime.fromisoformat(last_str)
        return datetime.now(timezone.utc) - last_dt < timedelta(hours=REPAIR_COOLDOWN_HOURS)
    except (ValueError, TypeError):
        return False


def _build_prompt(
    flow_name: str,
    primary_script: str,
    state_message: str,
    log_snippet: str | None,
    related_scripts: list[str],
    worktree_path: Path,
) -> str:
    related_lines = (
        "\n".join(f"- {worktree_path}/twitter/{s}" for s in related_scripts)
        if related_scripts
        else "None identified"
    )
    return f"""Fix a failing Twitter automation service. Make minimal, targeted changes only.

## Failed service
Prefect flow: {flow_name}
Primary script: {worktree_path}/twitter/{primary_script}

## Error / state message
{state_message}

## Recent log output (last 50 lines from Prefect)
{log_snippet or "(no log snippet available)"}

## Related scripts that may have the same issue (check after fixing primary)
{related_lines}

## Repository layout
- Main scripts: {worktree_path}/twitter/*.py
- Shared lib: {worktree_path}/lib/
- Tests: {worktree_path}/twitter/tests/

## Your task
1. Run the test suite first to establish a baseline:
     cd {worktree_path}/twitter && uv run pytest tests/ -q -m "not integration"
2. Read {worktree_path}/twitter/{primary_script} and any shared modules it imports from {worktree_path}/lib/
3. Identify the root cause from the error and log above
4. Make the minimal fix — change only what is necessary to fix the specific error
5. Check the related scripts listed above for the same bug pattern; fix them too if found
6. Fix ALL test failures shown in step 1, even ones unrelated to the original error — this
   worktree is based on a git HEAD that may have pre-existing failures that need resolving
7. Do NOT refactor, do NOT add features, do NOT change unrelated code

## Verify your fix
Run this command and ensure ALL tests pass (zero failures):
  cd {worktree_path}/twitter && uv run pytest tests/ -q -m "not integration"

You MUST run the tests and fix every failure before finishing.

## Summary
End with a one-paragraph summary: what was broken, exactly what you changed, which files.
"""


def _build_retry_prompt(
    flow_name: str,
    primary_script: str,
    attempt: int,
    test_output: str,
    worktree_path: Path,
) -> str:
    # Trim test output to avoid huge prompts
    trimmed = test_output[-3000:] if len(test_output) > 3000 else test_output
    return f"""Previous repair attempt {attempt} for '{flow_name}' fixed the flow but the test suite still has failures.

## Test output from attempt {attempt}
{trimmed}

## Your task
1. Read the failing test(s) and the source files they test
2. Fix only the root cause — do NOT rewrite tests to skip or hide failures
3. Run the tests again and confirm they pass:
   cd {worktree_path}/twitter && uv run pytest tests/ -q -m "not integration"

Primary script: {worktree_path}/twitter/{primary_script}

Be minimal. Touch only what is needed to make the tests green.

## Summary
End with a one-paragraph summary: what was broken, exactly what you changed, which files.
"""


def _summarize_test_output(output: str, passed: bool) -> str:
    """Extract a short summary line from pytest output."""
    lines = output.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if line and ("passed" in line or "failed" in line or "error" in line):
            return line
    return "tests passed" if passed else "tests failed"


# ---------------------------------------------------------------------------
# Main repair logic
# ---------------------------------------------------------------------------


def repair(
    flow_name: str,
    flow_run_id: str,
    state_message: str,
    log_snippet: str | None,
) -> int:
    """Run the repair workflow. Returns 0 on success/merge, 1 otherwise."""

    print(f"[repair] Starting repair for flow={flow_name} run={flow_run_id}", flush=True)

    # Ensure log dir exists
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Acquire lock
    # ------------------------------------------------------------------
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, BlockingIOError):
        print("[repair] Another repair is already running, exiting.", flush=True)
        os.close(lock_fd)
        return 0

    try:
        return _do_repair(flow_name, flow_run_id, state_message, log_snippet)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _do_repair(
    flow_name: str,
    flow_run_id: str,
    state_message: str,
    log_snippet: str | None,
) -> int:
    start_time = time.time()
    log_snippet = log_snippet or ""

    # ------------------------------------------------------------------
    # 2. Signal-exit guard
    # ------------------------------------------------------------------
    if _is_signal_exit(state_message):
        print(
            f"[repair] Skipping — state message indicates a signal exit: {state_message[:120]}",
            flush=True,
        )
        send_telegram(
            f"Twitter repair skipped for {flow_name}: signal exit, not a code bug.\n"
            f"Message: {state_message[:300]}"
        )
        return 0

    # ------------------------------------------------------------------
    # 2b. Cooldown check
    # ------------------------------------------------------------------
    state = load_state(STATE_FILE, _STATE_DEFAULT)
    if _check_cooldown(state, flow_name):
        print(
            f"[repair] Cooldown active for {flow_name} (last repair < {REPAIR_COOLDOWN_HOURS}h ago).",
            flush=True,
        )
        send_telegram(
            f"Twitter repair skipped for {flow_name}: cooldown active "
            f"(last repair within {REPAIR_COOLDOWN_HOURS}h)."
        )
        return 0

    # ------------------------------------------------------------------
    # 3. Resolve primary script
    # ------------------------------------------------------------------
    primary_script = FLOW_TO_SCRIPT.get(flow_name)
    if not primary_script:
        print(f"[repair] Unknown flow_name={flow_name}, cannot map to script.", flush=True)
        send_telegram(f"Twitter repair failed: unknown flow '{flow_name}'.")
        return 1

    # ------------------------------------------------------------------
    # 4. Related scripts
    # ------------------------------------------------------------------
    related = _related_scripts(flow_name, log_snippet)

    # ------------------------------------------------------------------
    # 5. Create git worktree
    # ------------------------------------------------------------------
    branch = f"repair-{int(time.time())}"
    worktree_path = WORKTREES_DIR / branch
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[repair] Creating worktree at {worktree_path} on branch {branch}", flush=True)
    wt_result = subprocess.run(
        ["git", "-C", str(AUTOMATIONS_DIR), "worktree", "add", str(worktree_path), "-b", branch],
        capture_output=True,
        text=True,
    )
    if wt_result.returncode != 0:
        print(f"[repair] git worktree add failed: {wt_result.stderr}", flush=True)
        send_telegram(
            f"Twitter repair failed for {flow_name}: could not create git worktree.\n"
            f"{wt_result.stderr[:300]}"
        )
        return 1

    # Symlink .venv if present in main twitter dir
    main_venv = AUTOMATIONS_DIR / "twitter" / ".venv"
    wt_venv = worktree_path / "twitter" / ".venv"
    if main_venv.exists() and not wt_venv.exists():
        try:
            wt_venv.symlink_to(main_venv)
            print(f"[repair] Symlinked .venv: {wt_venv} -> {main_venv}", flush=True)
        except Exception as e:
            print(f"[repair] Warning: could not symlink .venv: {e}", flush=True)

    # ------------------------------------------------------------------
    # 6–8. Run opencode → test suite, with retries on test failure
    # ------------------------------------------------------------------
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    # Allow opencode to access the .venv symlink (which points outside the worktree)
    env["OPENCODE_PERMISSIONS"] = '{"*": {"*": "allow"}}'

    opencode_exit = 0
    opencode_output = ""
    tests_passed = False
    test_output = ""
    test_summary = "no tests run"

    for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
        if attempt == 1:
            prompt = _build_prompt(
                flow_name, primary_script, state_message, log_snippet, related, worktree_path
            )
        else:
            print(
                f"[repair] Tests failed on attempt {attempt - 1}; retrying opencode with test output …",
                flush=True,
            )
            prompt = _build_retry_prompt(
                flow_name, primary_script, attempt - 1, test_output, worktree_path
            )

        print(f"[repair] Running opencode attempt {attempt}/{MAX_REPAIR_ATTEMPTS} (timeout 3600s) …", flush=True)
        try:
            oc_result = subprocess.run(
                ["timeout", "3600", OPENCODE_BIN, "run", "--thinking", prompt, "--dir", str(worktree_path)],
                env=env,
                capture_output=True,
                text=True,
                timeout=3660,
                cwd=str(worktree_path),
            )
        except subprocess.TimeoutExpired:
            oc_result = type("R", (), {"returncode": 124, "stdout": "", "stderr": "timeout"})()

        opencode_exit = oc_result.returncode
        opencode_output = (oc_result.stdout or "") + "\n" + (oc_result.stderr or "")
        print(f"[repair] opencode exited with code {opencode_exit}", flush=True)

        print(f"[repair] Running test suite (attempt {attempt}) …", flush=True)
        test_result = subprocess.run(
            ["uv", "run", "pytest", "tests/", "-q", "-m", "not integration"],
            cwd=str(worktree_path / "twitter"),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        tests_passed = test_result.returncode == 0
        test_output = (test_result.stdout or "") + (test_result.stderr or "")
        test_summary = _summarize_test_output(test_output, tests_passed)
        print(
            f"[repair] Tests {'PASSED' if tests_passed else 'FAILED'} (attempt {attempt}): {test_summary}",
            flush=True,
        )

        if tests_passed:
            break

    # ------------------------------------------------------------------
    # 9 & 10. Merge or leave worktree
    # ------------------------------------------------------------------
    merged = False
    if tests_passed:
        # Try fast-forward first
        ff_result = subprocess.run(
            ["git", "-C", str(AUTOMATIONS_DIR), "merge", "--ff-only", branch],
            capture_output=True,
            text=True,
        )
        if ff_result.returncode == 0:
            merged = True
            print("[repair] Fast-forward merge succeeded.", flush=True)
        else:
            # Fall back to no-ff merge
            noff_result = subprocess.run(
                [
                    "git", "-C", str(AUTOMATIONS_DIR),
                    "merge", "--no-ff", branch,
                    "-m", f"fix: auto-repair {flow_name}",
                ],
                capture_output=True,
                text=True,
            )
            if noff_result.returncode == 0:
                merged = True
                print("[repair] No-ff merge succeeded.", flush=True)
            else:
                print(
                    f"[repair] Merge failed: {noff_result.stderr[:300]}",
                    flush=True,
                )

    # Cleanup worktree only if merged
    if merged:
        subprocess.run(
            ["git", "-C", str(AUTOMATIONS_DIR), "worktree", "remove", "--force", str(worktree_path)],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(AUTOMATIONS_DIR), "branch", "-d", branch],
            capture_output=True,
        )
    else:
        print(f"[repair] Worktree left at {worktree_path} for inspection.", flush=True)

    # ------------------------------------------------------------------
    # 11. Send Telegram summary
    # ------------------------------------------------------------------
    duration = int(time.time() - start_time)
    status_str = "FIXED & MERGED" if merged else "FAILED (tests did not pass)"
    detail = "Fix merged to master." if merged else opencode_output[-500:]

    tg_msg = (
        f"Auto-repair: {flow_name}\n\n"
        f"Status: {status_str}\n"
        f"Tests: {test_summary}\n"
        f"Duration: {duration}s\n"
        f"Branch: {branch}\n\n"
        f"{detail}"
    )
    send_telegram(tg_msg)

    # ------------------------------------------------------------------
    # 12. Save cooldown + append to repair log
    # ------------------------------------------------------------------
    # Reload state in case it was updated while opencode ran
    state = load_state(STATE_FILE, _STATE_DEFAULT)
    if "lastRepairs" not in state:
        state["lastRepairs"] = {}
    if "repairHistory" not in state:
        state["repairHistory"] = []

    state["lastRepairs"][flow_name] = _now_iso()

    history_entry = {
        "timestamp": _now_iso(),
        "flowName": flow_name,
        "flowRunId": flow_run_id,
        "errorSummary": state_message[:200],
        "opencodeExitCode": opencode_exit,
        "testResult": test_summary,
        "merged": merged,
        "worktreeBranch": branch,
        "durationSeconds": duration,
    }
    state["repairHistory"].append(history_entry)
    # Cap at MAX_HISTORY
    if len(state["repairHistory"]) > MAX_HISTORY:
        state["repairHistory"] = state["repairHistory"][-MAX_HISTORY:]

    save_state(STATE_FILE, state)

    # ------------------------------------------------------------------
    # 13. Exit code
    # ------------------------------------------------------------------
    return 0 if merged else 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Twitter automation service repair script"
    )
    parser.add_argument("--flow-name", required=True, help="Prefect flow name")
    parser.add_argument("--flow-run-id", required=True, help="Prefect flow run UUID")
    parser.add_argument("--state-message", required=True, help="Prefect state message / error")
    parser.add_argument("--log-snippet", default="", help="Recent log output (last 50 lines)")
    args = parser.parse_args()

    return repair(
        flow_name=args.flow_name,
        flow_run_id=args.flow_run_id,
        state_message=args.state_message,
        log_snippet=args.log_snippet,
    )


if __name__ == "__main__":
    sys.exit(main())

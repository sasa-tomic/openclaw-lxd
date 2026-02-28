#!/usr/bin/env python3
"""Consolidated Twitter automation scheduler using Prefect.

Replaces 7 systemd services with a single Prefect deployment.

Setup:
  1. uv sync
  2. prefect server start  (in one terminal)
  3. prefect worker start --pool twitter-automation  (in another)
  4. prefect deploy --all  (to register schedules)
  5. View UI at http://localhost:4200

Or run ad-hoc:
  uv run python twitter_scheduler.py <flow>
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

from prefect import flow, get_run_logger

TWITTER_DIR = Path("/projects/automations/twitter")
PYTHON = sys.executable

_REPAIR_SCRIPT = Path(__file__).parent / "repair_service.py"


def _get_prefect_logs(flow_run_id, limit=50) -> str:
    """Fetch recent log lines from the local Prefect API."""
    import urllib.request

    url = "http://127.0.0.1:4200/api/logs/filter"
    payload = json.dumps({
        "logs": {"flow_run_id": {"any_": [str(flow_run_id)]}},
        "limit": limit,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logs = json.loads(resp.read())
            return "\n".join(
                f"[{'ERROR' if l.get('level', 0) >= 40 else 'INFO'}] {l.get('message', '')[:300]}"
                for l in logs
            )
    except Exception:
        return ""


def _on_flow_failure(flow, flow_run, state):
    """Auto-repair hook: fires on flow failure or crash. Non-blocking."""
    log_snippet = _get_prefect_logs(flow_run.id)
    log_path = Path("/home/openclaw/clawd/logs/twitter-repair.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("a")
    try:
        subprocess.Popen(
            [
                sys.executable, "-u",
                str(_REPAIR_SCRIPT),
                "--flow-name", flow.name,
                "--flow-run-id", str(flow_run.id),
                "--state-message", str(state.message or ""),
                "--log-snippet", log_snippet,
            ],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(Path(__file__).parent),
            close_fds=True,
        )
    finally:
        log_fh.close()


def _stream_pipe(pipe, log_fn, print_fn):
    """Drain a pipe line-by-line, calling log_fn (or print_fn if no logger)."""
    for raw_line in pipe:
        line = raw_line.rstrip()
        if line:
            if log_fn:
                log_fn(line)
            else:
                print_fn(line)


def run_script(script_path: Path, timeout: int = 3600, logger=None) -> int:
    """Run a Python script, streaming stdout→info and stderr→error to the logger."""
    process = subprocess.Popen(
        [PYTHON, "-u", str(script_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=script_path.parent,
    )

    # Drain stdout and stderr concurrently so neither blocks the other.
    stdout_thread = threading.Thread(
        target=_stream_pipe,
        args=(
            process.stdout,
            logger.info if logger else None,
            lambda line: print(line, flush=True),
        ),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_stream_pipe,
        args=(
            process.stderr,
            logger.error if logger else None,
            lambda line: print(line, file=sys.stderr, flush=True),
        ),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        if logger:
            logger.error(f"Script timed out after {timeout}s")
        return 124
    except Exception as e:
        if logger:
            logger.error(f"Script error: {e}")
        return 1
    finally:
        stdout_thread.join()
        stderr_thread.join()

    rc = process.returncode
    if rc != 0:
        if logger:
            logger.error(f"{script_path.name} exited with code {rc}")
        raise RuntimeError(f"{script_path.name} exited with code {rc}")
    return rc


@flow(name="twitter-engagement", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def engagement_flow():
    """Autonomous Twitter engagement - hourly."""
    logger = get_run_logger()
    delay = random.randint(0, 3000)
    logger.info(f"Jitter: sleeping {delay}s before engagement")
    time.sleep(delay)
    logger.info("Starting engagement run")
    code = run_script(TWITTER_DIR / "twitter_engagement.py", logger=logger)
    logger.info(f"Engagement completed with code {code}")
    return code


@flow(name="twitter-original-content", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def original_content_flow():
    """Post original content - 2x daily."""
    logger = get_run_logger()
    logger.info("Starting original content post")
    code = run_script(TWITTER_DIR / "post_original_content.py", logger=logger)
    logger.info(f"Original content completed with code {code}")
    return code


@flow(name="twitter-weekly-thread", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def weekly_thread_flow():
    """Post weekly thread - Wed 15:00 UTC."""
    logger = get_run_logger()
    logger.info("Starting weekly thread post")
    code = run_script(TWITTER_DIR / "post_thread.py", timeout=1800, logger=logger)
    logger.info(f"Weekly thread completed with code {code}")
    return code


@flow(name="twitter-daily-eval", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def daily_eval_flow():
    """Daily strategy evaluation - 7:00 UTC."""
    logger = get_run_logger()
    logger.info("Starting daily strategy eval")
    code = run_script(TWITTER_DIR / "daily_strategy_eval.py", logger=logger)
    logger.info(f"Daily eval completed with code {code}")
    return code


@flow(name="twitter-morning-research", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def morning_research_flow():
    """Morning research - 8:00 UTC."""
    logger = get_run_logger()
    logger.info("Starting morning research")
    code = run_script(TWITTER_DIR / "twitter_morning.py", logger=logger)
    logger.info(f"Morning research completed with code {code}")
    return code


@flow(name="twitter-cdp-health", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def cdp_health_flow():
    """CDP health check - every 15 min."""
    logger = get_run_logger()
    logger.info("Starting CDP health check")
    code = run_script(TWITTER_DIR / "cdp_health_check.py", timeout=60, logger=logger)
    logger.info(f"CDP health check completed with code {code}")
    return code


@flow(name="twitter-reply-monitor", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def reply_monitor_flow():
    """Monitor and respond to replies/mentions — every 5 min."""
    logger = get_run_logger()
    logger.info("Starting reply monitor")
    code = run_script(TWITTER_DIR / "reply_monitor.py", timeout=300, logger=logger)
    logger.info(f"Reply monitor completed with code {code}")
    return code



@flow(name="twitter-target-monitor", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def target_monitor_flow():
    """Monitor target accounts for fast-reply opportunities -- every 30 min."""
    logger = get_run_logger()
    logger.info("Starting target account monitor")
    code = run_script(TWITTER_DIR / "target_monitor.py", timeout=600, logger=logger)
    logger.info(f"Target monitor completed with code {code}")
    return code


@flow(name="twitter-timeline-monitor", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def timeline_monitor_flow():
    """Monitor Following feed for near-realtime reply opportunities — every 5 min."""
    logger = get_run_logger()
    logger.info("Starting timeline monitor")
    code = run_script(TWITTER_DIR / "timeline_monitor.py", timeout=300, logger=logger)
    logger.info(f"Timeline monitor completed with code {code}")
    return code


@flow(name="twitter-search-queue", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def search_queue_flow():
    """Fill candidate queue via CDP search — every hour."""
    logger = get_run_logger()
    logger.info("Starting search queue fill")
    code = run_script(TWITTER_DIR / "search_queue.py", timeout=300, logger=logger)
    logger.info(f"Search queue completed with code {code}")
    return code


@flow(name="twitter-account-discovery", log_prints=True, on_failure=[_on_flow_failure], on_crashed=[_on_flow_failure])
def account_discovery_flow():
    """Discover and score new candidate accounts — daily."""
    logger = get_run_logger()
    logger.info("Starting account discovery")
    code = run_script(TWITTER_DIR / "account_discovery.py", timeout=1800, logger=logger)
    logger.info(f"Account discovery completed with code {code}")
    return code


FLOWS = {
    "engagement": engagement_flow,
    "content": original_content_flow,
    "thread": weekly_thread_flow,
    "eval": daily_eval_flow,
    "research": morning_research_flow,
    "health": cdp_health_flow,
    "monitor": reply_monitor_flow,
    "targets": target_monitor_flow,
    "timeline": timeline_monitor_flow,
    "discovery": account_discovery_flow,
    "search-queue": search_queue_flow,
}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Twitter automation scheduler")
    parser.add_argument(
        "flow", nargs="?", choices=list(FLOWS.keys()), help="Run specific flow"
    )
    parser.add_argument("--all", action="store_true", help="Run all flows once")
    args = parser.parse_args()

    if args.all:
        print("Running all flows once...")
        for name, flow_fn in FLOWS.items():
            print(f"\n=== {name} ===")
            flow_fn()
    elif args.flow:
        FLOWS[args.flow]()
    else:
        print("Twitter Scheduler - Prefect")
        print("Schedules: http://192.168.0.13:4200")
        print("\nFlows:", ", ".join(FLOWS))
        print("\nUsage:")
        print("  uv run python twitter_scheduler.py <flow>  # Run specific flow")
        print("  uv run python twitter_scheduler.py --all   # Run all once")
        print("\nDeploy with schedules:")
        print("  uv run prefect deploy --all")

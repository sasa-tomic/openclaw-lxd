#!/usr/bin/env python3
"""CDP health check for Twitter automation.

Checks that Chrome CDP at localhost:9222 is reachable and responsive.
Sends Telegram alerts on failure and recovery. Tracks state between runs
in Postgres kv_state.

Independent of twitter_utils.py -- works even if other code is broken.
"""

import json
import logging
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)
from lib.config import OPENCLAW_BIN, TELEGRAM_TARGET
from lib.telegram_utils import send_telegram
from twitter.db import ensure_schema, get_cdp_health_state, get_conn, set_cdp_health_state
STATE_DEFAULT = {"down": False, "since": None, "last_check": None}
CDP_URL = "http://localhost:9222/json/version"
CDP_TIMEOUT = 10
SOCAT_SERVICE = "socat-proxy.service"


def load_health_state() -> dict:
    """Load health-check state from DB, falling back to defaults."""
    try:
        with get_conn() as conn:
            ensure_schema(conn)
            return get_cdp_health_state(conn)
    except Exception as e:
        logger.warning(f"Could not load health state from DB: {e}")
        return dict(STATE_DEFAULT)


def save_health_state(state: dict) -> None:
    """Save health-check state to DB (best effort)."""
    try:
        with get_conn() as conn:
            ensure_schema(conn)
            set_cdp_health_state(
                conn,
                down=bool(state.get("down", False)),
                since=state.get("since"),
                last_check=state.get("last_check"),
            )
    except Exception as e:
        logger.warning(f"Could not save health state to DB: {e}")


def send_telegram_alert(message: str) -> None:
    logger.info(f"Sending Telegram alert: {message[:80]}...")
    send_telegram(message)


def check_socat() -> tuple[bool, str]:
    """Check if socat-proxy.service is active. Returns (active, status_text)."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", SOCAT_SERVICE],
            capture_output=True,
            text=True,
            timeout=10,
        )
        status = result.stdout.strip()
        return status == "active", status
    except Exception as e:
        return False, f"error: {e}"


def check_cdp() -> tuple[bool, str]:
    """Check CDP endpoint. Returns (ok, detail)."""
    try:
        req = urllib.request.Request(CDP_URL)
        with urllib.request.urlopen(req, timeout=CDP_TIMEOUT) as resp:
            data = resp.read()
            if not data:
                return False, "empty reply"
            body = json.loads(data)
            browser = body.get("Browser", "unknown")
            return True, f"ok ({browser})"
    except urllib.error.URLError as e:
        return False, f"connection error: {e.reason}"
    except TimeoutError:
        return False, "timeout"
    except json.JSONDecodeError:
        return False, "invalid JSON response"
    except Exception as e:
        return False, f"error: {e}"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    logger.info("Starting health check")

    state = load_health_state()
    was_down = state.get("down", False)

    socat_ok, socat_status = check_socat()
    logger.info(f"socat-proxy.service: {socat_status}")

    cdp_ok, cdp_detail = check_cdp()
    logger.info(f"CDP {CDP_URL}: {cdp_detail}")

    healthy = socat_ok and cdp_ok

    if healthy:
        if was_down:
            down_since = state.get("since", "unknown")
            send_telegram_alert(
                f"[CDP Health] RECOVERED\n"
                f"Chrome CDP is back online.\n"
                f"Was down since: {down_since}\n"
                f"socat: {socat_status} | CDP: {cdp_detail}"
            )
            logger.info("Status: RECOVERED (was down, now up)")
        else:
            logger.info("Status: HEALTHY")
        state["down"] = False
        state["since"] = None

    else:
        problems = []
        if not socat_ok:
            problems.append(f"socat-proxy: {socat_status}")
        if not cdp_ok:
            problems.append(f"CDP endpoint: {cdp_detail}")
        problem_text = " | ".join(problems)

        if not was_down:
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            send_telegram_alert(
                f"[CDP Health] DOWN\n"
                f"Chrome CDP is unreachable. All Twitter automation will fail.\n"
                f"{problem_text}\n"
                f"Host: 192.168.0.13:9222 via socat localhost:9222"
            )
            state["down"] = True
            state["since"] = now
            logger.warning(f"Status: DOWN (new failure: {problem_text})")
        else:
            logger.warning(
                f"Status: STILL DOWN since {state.get('since', '?')} ({problem_text})"
            )

    state["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_health_state(state)
    logger.info("Done")


if __name__ == "__main__":
    main()
    sys.exit(0)

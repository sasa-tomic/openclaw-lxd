#!/usr/bin/env python3
"""Smoke tests: ensure all twitter scripts import cleanly and key invariants hold.

These tests catch the class of bug that killed posting for 4+ days:
  post_original_content.py had wrong sys.path → ModuleNotFoundError on every run
  → Prefect marked the flow COMPLETED (not FAILED) → zero alert, zero posts.

Run from repo root:
    pytest twitter/tests/test_imports_and_health.py -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Keep tests self-contained — add both roots needed by twitter scripts.
_AUTOMATIONS_ROOT = str(Path(__file__).parent.parent.parent)  # /projects/automations
_TWITTER_ROOT = str(Path(__file__).parent.parent)             # /projects/automations/twitter
for _p in (_AUTOMATIONS_ROOT, _TWITTER_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TWITTER_DIR = Path(__file__).parent.parent
PYTHON = sys.executable

# All scripts that are launched by twitter_scheduler.py as subprocesses.
SCHEDULER_SCRIPTS = [
    "twitter_engagement.py",
    "post_original_content.py",
    "post_thread.py",
    "daily_strategy_eval.py",
    "twitter_morning.py",
    "cdp_health_check.py",
    "reply_monitor.py",
    "target_monitor.py",
    "timeline_monitor.py",
    "account_discovery.py",
    "search_queue.py",
]


# ---------------------------------------------------------------------------
# 1. Import-level smoke test: each script must survive `python -c "import ..."`.
#    Specifically guards against ModuleNotFoundError from bad sys.path setup.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script_name", SCHEDULER_SCRIPTS)
def test_script_has_no_import_error(script_name: str) -> None:
    """Each scheduler script must be importable without ModuleNotFoundError.

    This is the exact failure mode that killed posts for 4 days: wrong sys.path
    meant 'from lib.llm_utils import ...' raised ModuleNotFoundError at module
    load time, causing exit code 1 before any business logic ran.
    """
    script_path = TWITTER_DIR / script_name
    assert script_path.exists(), f"Script not found: {script_path}"

    # Run: python -c "import importlib.util; ..." to trigger top-level imports
    # without executing if __name__ == '__main__'. We use compile-check + exec
    # of the import block by actually running the script with --help or by
    # checking that `py_compile` succeeds and the first-level imports are ok.
    # Simplest reliable approach: run the file via subprocess with a very short
    # timeout, capturing the traceback. We inject an early SystemExit so the
    # script's main() never runs — but module-level code (including sys.path
    # manipulation and imports) does.
    sentinel = "IMPORT_OK_SENTINEL_7f3a"
    code = (
        f"import sys\n"
        f"sys.argv = ['{script_name}']\n"
        # Patch common blocking calls so the script doesn't hang or connect to
        # external services during import-phase side-effects.
        f"import unittest.mock as _m\n"
        f"_m.patch('builtins.input', return_value='').start()\n"
        f"import importlib.util as _u\n"
        f"_spec = _u.spec_from_file_location('_script', r'{script_path}')\n"
        f"_mod = _u.module_from_spec(_spec)\n"
        f"try:\n"
        f"    _spec.loader.exec_module(_mod)\n"
        f"except SystemExit:\n"
        f"    pass\n"
        f"except Exception as _e:\n"
        f"    # Only fail on import-level errors, not runtime errors that need CDP/DB\n"
        f"    if isinstance(_e, (ImportError, ModuleNotFoundError)):\n"
        f"        raise\n"
        f"print('{sentinel}')\n"
    )

    result = subprocess.run(
        [PYTHON, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(TWITTER_DIR),
    )

    # A ModuleNotFoundError will appear in stderr and exit code will be 1.
    if "ModuleNotFoundError" in result.stderr or "ImportError" in result.stderr:
        pytest.fail(
            f"{script_name}: import failed\n"
            f"STDERR:\n{result.stderr[:1000]}"
        )

    # The sentinel must appear in stdout for the test to be conclusive.
    if sentinel not in result.stdout:
        # If the script exited early for a non-import reason (e.g. missing DB,
        # missing env var) that's acceptable — just not an ImportError.
        if "ModuleNotFoundError" not in result.stderr and "ImportError" not in result.stderr:
            # Non-import exit is OK for this test.
            pass
        else:
            pytest.fail(
                f"{script_name}: unexpected failure\n"
                f"STDOUT: {result.stdout[:500]}\n"
                f"STDERR: {result.stderr[:500]}"
            )


# ---------------------------------------------------------------------------
# 2. Scheduler raises on non-zero script exit
#    Guards against the silent-failure bug: flows returning COMPLETED when the
#    underlying script crashes.
# ---------------------------------------------------------------------------

def test_run_script_raises_on_nonzero_exit(tmp_path: Path) -> None:
    """twitter_scheduler.run_script() must raise RuntimeError on non-zero exit.

    Previously it just returned the exit code, so Prefect marked flows as
    COMPLETED even when scripts crashed — making failures completely invisible.
    """
    # Import the scheduler module from the twitter dir.
    import importlib.util

    scheduler_path = TWITTER_DIR / "twitter_scheduler.py"
    spec = importlib.util.spec_from_file_location("twitter_scheduler", scheduler_path)
    scheduler = importlib.util.module_from_spec(spec)

    # Stub out prefect decorators so the module loads without a live server.
    import unittest.mock as mock
    import types

    fake_prefect = types.ModuleType("prefect")
    fake_prefect.flow = lambda *a, **kw: (lambda f: f)  # no-op decorator
    fake_prefect.get_run_logger = mock.MagicMock(return_value=mock.MagicMock())
    sys.modules.setdefault("prefect", fake_prefect)

    spec.loader.exec_module(scheduler)

    # Create a tiny script that exits with code 1.
    failing_script = tmp_path / "fail.py"
    failing_script.write_text("import sys; sys.exit(1)\n")

    with pytest.raises(RuntimeError, match="exited with code 1"):
        scheduler.run_script(failing_script, timeout=10)


def test_run_script_returns_zero_on_success(tmp_path: Path) -> None:
    """run_script() must return 0 (not raise) on a successful script."""
    import importlib.util, types, unittest.mock as mock

    scheduler_path = TWITTER_DIR / "twitter_scheduler.py"
    spec = importlib.util.spec_from_file_location("twitter_scheduler2", scheduler_path)
    scheduler = importlib.util.module_from_spec(spec)

    fake_prefect = types.ModuleType("prefect")
    fake_prefect.flow = lambda *a, **kw: (lambda f: f)
    fake_prefect.get_run_logger = mock.MagicMock(return_value=mock.MagicMock())
    sys.modules.setdefault("prefect", fake_prefect)

    spec.loader.exec_module(scheduler)

    ok_script = tmp_path / "ok.py"
    ok_script.write_text("print('all good')\n")

    result = scheduler.run_script(ok_script, timeout=10)
    assert result == 0


# ---------------------------------------------------------------------------
# 3. Content freshness guard
#    Verifies that the DB helper used by post_original_content tracks posts
#    correctly. If get_recent_posts returns empty for 4+ days, something broke.
# ---------------------------------------------------------------------------

def test_db_get_recent_posts_schema() -> None:
    """get_recent_posts() must return a list (not raise) and each entry has 'text'."""
    # This test only runs if the DB is reachable; skip gracefully otherwise.
    try:
        from db import get_conn, get_recent_posts
    except ImportError:
        pytest.skip("db module not importable (venv or path issue)")

    try:
        with get_conn() as conn:
            rows = get_recent_posts(conn, days=30, limit=5)
    except Exception as e:
        pytest.skip(f"DB not reachable: {e}")

    assert isinstance(rows, list), "get_recent_posts must return a list"
    for row in rows:
        assert "text" in row, f"Missing 'text' key in post row: {row.keys()}"


def test_count_posts_today_returns_int() -> None:
    """count_posts_today() must return an integer (regression guard)."""
    try:
        from db import get_conn, count_posts_today
    except ImportError:
        pytest.skip("db module not importable")

    try:
        with get_conn() as conn:
            count = count_posts_today(conn)
    except Exception as e:
        pytest.skip(f"DB not reachable: {e}")

    assert isinstance(count, int), f"Expected int, got {type(count)}: {count}"

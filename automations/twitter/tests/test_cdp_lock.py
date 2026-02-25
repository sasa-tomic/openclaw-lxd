#!/usr/bin/env python3
"""Tests for cdp_lock() file-based locking in twitter_utils.py.

Tests verify:
1. Lock file is created when cdp_lock() is acquired
2. Concurrent lock attempts serialize (second waits for first to release)
3. Lock is released on exception (exception safety)
4. Lock timeout raises TimeoutError when held too long
"""
import fcntl
import multiprocessing
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from twitter.twitter_utils import CDP_LOCK_PATH, CDP_LOCK_TIMEOUT, cdp_lock


# ---------------------------------------------------------------------------
# Helper: acquire lock in a subprocess
# ---------------------------------------------------------------------------

def _hold_lock_for(duration_sec: float, result_queue: multiprocessing.Queue) -> None:
    """Subprocess target: acquire cdp_lock, hold it, then release."""
    try:
        with cdp_lock():
            result_queue.put(("acquired", time.monotonic()))
            time.sleep(duration_sec)
            result_queue.put(("released", time.monotonic()))
    except Exception as e:
        result_queue.put(("error", str(e)))


# ---------------------------------------------------------------------------
# Test 1: Lock file is created
# ---------------------------------------------------------------------------

def test_lock_file_is_created():
    """cdp_lock() must create the lock file at CDP_LOCK_PATH."""
    # Remove if leftover from a previous test run
    if CDP_LOCK_PATH.exists():
        CDP_LOCK_PATH.unlink()

    with cdp_lock():
        assert CDP_LOCK_PATH.exists(), f"Lock file not created at {CDP_LOCK_PATH}"

    print("  PASS: test_lock_file_is_created")


# ---------------------------------------------------------------------------
# Test 2: Lock serializes concurrent access
# ---------------------------------------------------------------------------

def test_concurrent_lock_serializes():
    """A second cdp_lock() attempt must wait until the first is released.

    Flow:
      1. Subprocess P1 acquires lock, holds for 2s.
      2. Main process waits briefly, then tries to acquire lock (should block).
      3. Main process measures how long it waited — must be >= hold duration.
    """
    hold_sec = 2.0

    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    p1 = multiprocessing.Process(
        target=_hold_lock_for, args=(hold_sec, result_queue), daemon=True
    )
    p1.start()

    # Wait for P1 to confirm it has the lock
    event, p1_acquired_at = result_queue.get(timeout=10)
    assert event == "acquired", f"P1 did not acquire lock: {event}"

    # Now try to acquire from main process — should block
    wait_start = time.monotonic()
    with cdp_lock():
        wait_end = time.monotonic()
        wait_duration = wait_end - wait_start

    # Verify we waited at least (hold_sec - 1.5s tolerance for scheduling jitter)
    assert wait_duration >= (hold_sec - 1.5), (
        f"Expected to wait >= {hold_sec - 1.5:.1f}s but only waited {wait_duration:.2f}s"
    )

    p1.join(timeout=10)
    print(f"  PASS: test_concurrent_lock_serializes (waited {wait_duration:.2f}s for {hold_sec}s hold)")


# ---------------------------------------------------------------------------
# Test 3: Lock is released on exception
# ---------------------------------------------------------------------------

def test_lock_released_on_exception():
    """cdp_lock() must release the lock even when the body raises an exception."""
    try:
        with cdp_lock():
            raise ValueError("deliberate test exception")
    except ValueError:
        pass  # Expected

    # Now try to re-acquire in the same process — should succeed immediately
    # (flock is per open-file-description, so re-opening the file gives a fresh lock)
    acquired = False
    try:
        with cdp_lock():
            acquired = True
    except Exception as e:
        assert False, f"Re-acquire after exception failed: {e}"

    assert acquired, "Failed to re-acquire lock after exception release"
    print("  PASS: test_lock_released_on_exception")


# ---------------------------------------------------------------------------
# Test 4: Lock timeout (simulated — short timeout)
# ---------------------------------------------------------------------------

def _hold_lock_indefinitely(ready_event: multiprocessing.Event) -> None:
    """Subprocess: acquire lock and signal ready, then sleep forever."""
    with open(CDP_LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        ready_event.set()
        time.sleep(60)  # Hold until subprocess is killed


def test_lock_timeout():
    """cdp_lock() raises TimeoutError after CDP_LOCK_TIMEOUT seconds.

    Uses a patched timeout via monkeypatching the module constant so the
    test completes in a few seconds rather than 300s.
    """
    import twitter.twitter_utils as tu

    original_timeout = tu.CDP_LOCK_TIMEOUT
    tu.CDP_LOCK_TIMEOUT = 2  # Override to 2s for test speed

    ready_event = multiprocessing.Event()
    p = multiprocessing.Process(
        target=_hold_lock_indefinitely, args=(ready_event,), daemon=True
    )
    p.start()

    try:
        # Wait for the subprocess to hold the lock
        assert ready_event.wait(timeout=10), "Subprocess did not acquire lock in time"

        start = time.monotonic()
        raised_timeout = False
        try:
            with cdp_lock():
                pass
        except TimeoutError:
            raised_timeout = True
        elapsed = time.monotonic() - start

        assert raised_timeout, "TimeoutError was NOT raised — lock did not time out"
        assert elapsed >= 2.0, f"Timed out too fast: {elapsed:.2f}s (expected >= 2s)"
        print(f"  PASS: test_lock_timeout (timed out after {elapsed:.2f}s)")

    finally:
        p.terminate()
        p.join()
        tu.CDP_LOCK_TIMEOUT = original_timeout


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    multiprocessing.set_start_method("fork", force=True)

    print("Running cdp_lock() tests...")
    print(f"  Lock path: {CDP_LOCK_PATH}")
    print(f"  Lock timeout: {CDP_LOCK_TIMEOUT}s")
    print()

    tests = [
        test_lock_file_is_created,
        test_concurrent_lock_serializes,
        test_lock_released_on_exception,
        test_lock_timeout,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        print(f"Running {test_fn.__name__}...")
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {test_fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {test_fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
        print()

    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    else:
        print("All tests passed.")

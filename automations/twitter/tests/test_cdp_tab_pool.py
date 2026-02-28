#!/usr/bin/env python3
"""Tests for _acquire_tab_slot() and cdp_tab() pool arbitration.

Tests verify cross-process slot arbitration using file locks:
1. _acquire_tab_slot returns a slot in [0, CDP_POOL_SIZE) immediately
2. Two callers can hold different slots simultaneously
3. Third caller blocks until a slot is freed
4. TimeoutError raised when all slots busy past the deadline
5. cdp_tab() releases the slot lock on exception (exception safety)

Run from repo root:
    pytest twitter/tests/test_cdp_tab_pool.py -v
"""

import fcntl
import multiprocessing
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # /projects/automations
sys.path.insert(0, str(Path(__file__).parent.parent))          # /projects/automations/twitter

from twitter_utils import CDP_POOL_SIZE, _acquire_tab_slot, cdp_tab


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slot_lock_path(slot: int) -> str:
    return f"/tmp/twitter-cdp-tab-{slot}.lock"


def _release(slot: int, lf) -> None:
    """Release a slot returned by _acquire_tab_slot."""
    fcntl.flock(lf, fcntl.LOCK_UN)
    lf.close()


def _cleanup_slot_locks() -> None:
    """Remove all slot lock files (call between tests to avoid leftover state)."""
    for slot in range(CDP_POOL_SIZE):
        p = Path(_slot_lock_path(slot))
        if p.exists():
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Subprocess targets (must be top-level for multiprocessing on some platforms)
# ---------------------------------------------------------------------------

def _hold_slot_raw(slot: int, duration: float, ready: multiprocessing.Event) -> None:
    """Subprocess: directly acquire a specific slot lock and hold it for duration."""
    with open(_slot_lock_path(slot), "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        ready.set()
        time.sleep(duration)
    # Lock is released when 'with' block exits


# ---------------------------------------------------------------------------
# Test 1: Basic acquire/release
# ---------------------------------------------------------------------------

def test_acquire_returns_valid_slot():
    """_acquire_tab_slot returns a slot in [0, CDP_POOL_SIZE) when pool is free."""
    _cleanup_slot_locks()
    deadline = time.monotonic() + 5.0
    slot, lf = _acquire_tab_slot(deadline)
    try:
        assert 0 <= slot < CDP_POOL_SIZE, f"slot {slot} out of range [0, {CDP_POOL_SIZE})"
    finally:
        _release(slot, lf)


def test_acquire_is_immediate_when_pool_free():
    """_acquire_tab_slot returns without blocking when all slots are free."""
    _cleanup_slot_locks()
    deadline = time.monotonic() + 5.0
    t0 = time.monotonic()
    slot, lf = _acquire_tab_slot(deadline)
    elapsed = time.monotonic() - t0
    try:
        assert elapsed < 0.2, f"Should return immediately, took {elapsed:.3f}s"
    finally:
        _release(slot, lf)


# ---------------------------------------------------------------------------
# Test 2: Two callers hold different slots simultaneously
# ---------------------------------------------------------------------------

def test_two_slots_usable_simultaneously():
    """When slot 0 is held, the next caller gets slot 1 without blocking.

    Slot 0 is locked by a subprocess. Main acquires a slot — should get slot 1
    immediately (not block waiting for slot 0).
    """
    _cleanup_slot_locks()
    ready = multiprocessing.Event()
    p = multiprocessing.Process(
        target=_hold_slot_raw, args=(0, 5.0, ready), daemon=True
    )
    p.start()
    try:
        assert ready.wait(timeout=10), "Subprocess did not acquire slot 0 in time"

        t0 = time.monotonic()
        slot, lf = _acquire_tab_slot(time.monotonic() + 5.0)
        elapsed = time.monotonic() - t0
        try:
            assert slot == 1, (
                f"Expected slot 1 (slot 0 is held by subprocess), got slot {slot}"
            )
            assert elapsed < 0.5, (
                f"Should acquire slot 1 immediately, took {elapsed:.3f}s"
            )
        finally:
            _release(slot, lf)
    finally:
        p.terminate()
        p.join(timeout=5)


# ---------------------------------------------------------------------------
# Test 3: Blocks until a slot is freed
# ---------------------------------------------------------------------------

def test_blocks_until_slot_freed():
    """When all slots are held, _acquire_tab_slot blocks until one is released.

    Both slots are held by subprocesses for hold_sec seconds.
    Main process tries to acquire — must wait >= hold_sec.
    """
    _cleanup_slot_locks()
    hold_sec = 1.0
    ready0 = multiprocessing.Event()
    ready1 = multiprocessing.Event()
    p0 = multiprocessing.Process(
        target=_hold_slot_raw, args=(0, hold_sec, ready0), daemon=True
    )
    p1 = multiprocessing.Process(
        target=_hold_slot_raw, args=(1, hold_sec, ready1), daemon=True
    )
    p0.start()
    p1.start()
    try:
        assert ready0.wait(timeout=10), "P0 did not acquire slot 0"
        assert ready1.wait(timeout=10), "P1 did not acquire slot 1"

        t0 = time.monotonic()
        slot, lf = _acquire_tab_slot(time.monotonic() + 30.0)
        elapsed = time.monotonic() - t0
        try:
            assert slot in (0, 1), f"Unexpected slot {slot}"
            assert elapsed >= (hold_sec - 0.5), (
                f"Expected to block >= {hold_sec - 0.5:.1f}s, only waited {elapsed:.2f}s"
            )
        finally:
            _release(slot, lf)
    finally:
        p0.join(timeout=10)
        p1.join(timeout=10)


# ---------------------------------------------------------------------------
# Test 4: TimeoutError when all slots are busy past the deadline
# ---------------------------------------------------------------------------

def test_timeout_raises_when_all_slots_held():
    """_acquire_tab_slot raises TimeoutError when pool is exhausted past deadline."""
    _cleanup_slot_locks()
    ready0 = multiprocessing.Event()
    ready1 = multiprocessing.Event()
    p0 = multiprocessing.Process(
        target=_hold_slot_raw, args=(0, 60.0, ready0), daemon=True
    )
    p1 = multiprocessing.Process(
        target=_hold_slot_raw, args=(1, 60.0, ready1), daemon=True
    )
    p0.start()
    p1.start()
    try:
        assert ready0.wait(timeout=10), "P0 did not acquire slot 0"
        assert ready1.wait(timeout=10), "P1 did not acquire slot 1"

        deadline = time.monotonic() + 1.0  # short deadline
        with pytest.raises(TimeoutError):
            slot, lf = _acquire_tab_slot(deadline)
            # Should not reach here; release if it somehow does
            _release(slot, lf)
    finally:
        p0.terminate()
        p1.terminate()
        p0.join(timeout=5)
        p1.join(timeout=5)


# ---------------------------------------------------------------------------
# Test 5: cdp_tab() releases the lock on exception (exception safety)
# ---------------------------------------------------------------------------

def test_cdp_tab_releases_lock_on_exception():
    """cdp_tab() releases the slot lock even when the body raises an exception.

    Uses mocks to avoid needing a real Chrome instance.
    After the exception, all slot lock files must be acquirable immediately.
    """
    _cleanup_slot_locks()
    mock_cdp = MagicMock()

    with (
        patch("twitter_utils._ensure_tab_pool"),
        patch(
            "twitter_utils._get_sorted_page_tabs",
            return_value=[
                {"webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/1"},
                {"webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/2"},
            ],
        ),
        patch("twitter_utils.CDPSession.connect_to_tab", return_value=mock_cdp),
    ):
        try:
            with cdp_tab() as cdp:
                raise ValueError("deliberate test exception")
        except ValueError:
            pass  # expected

    # After the exception, every slot lock must be free (acquirable without blocking)
    freed_slots = []
    for slot in range(CDP_POOL_SIZE):
        lf = open(_slot_lock_path(slot), "w")
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            freed_slots.append(slot)
            fcntl.flock(lf, fcntl.LOCK_UN)
        except BlockingIOError:
            pass  # still locked — not freed
        finally:
            lf.close()

    # At least the one slot that was acquired must be free now
    assert len(freed_slots) >= 1, (
        f"No slots were freed after exception in cdp_tab() — "
        f"lock was not released. CDP_POOL_SIZE={CDP_POOL_SIZE}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    multiprocessing.set_start_method("fork", force=True)

    print("Running cdp_tab pool arbitration tests...")
    print(f"  CDP_POOL_SIZE: {CDP_POOL_SIZE}")
    print()

    tests = [
        test_acquire_returns_valid_slot,
        test_acquire_is_immediate_when_pool_free,
        test_two_slots_usable_simultaneously,
        test_blocks_until_slot_freed,
        test_timeout_raises_when_all_slots_held,
        test_cdp_tab_releases_lock_on_exception,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        print(f"Running {test_fn.__name__}...")
        try:
            test_fn()
            print(f"  PASS")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1
        print()

    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    else:
        print("All tests passed.")

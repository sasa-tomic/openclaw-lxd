"""Tests for reply_monitor own-account stats refresh."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, "/projects/automations/twitter")
sys.path.insert(0, "/projects/automations")

_twitter_utils_stub = mock.MagicMock()
_twitter_utils_stub.BLOCKED_AUTHORS = []
_lib_llm_stub = mock.MagicMock()

import importlib.util

MODULE_PATH = Path("/projects/automations/twitter/reply_monitor.py")

_saved_modules = {}
for _key, _stub in [
    ("twitter_utils", _twitter_utils_stub),
    ("lib", mock.MagicMock()),
    ("lib.llm_utils", _lib_llm_stub),
    ("lib.config", mock.MagicMock()),
]:
    _saved_modules[_key] = sys.modules.get(_key)
    sys.modules[_key] = _stub

spec = importlib.util.spec_from_file_location("reply_monitor", MODULE_PATH)
rm = importlib.util.module_from_spec(spec)
rm.call_llm = _lib_llm_stub.call_llm_simple
rm.extract_json = _lib_llm_stub.extract_json
spec.loader.exec_module(rm)

for _key, _orig in _saved_modules.items():
    if _orig is None:
        sys.modules.pop(_key, None)
    else:
        sys.modules[_key] = _orig
del _saved_modules, _key, _stub, _orig


def test_refresh_own_account_stats_persists_all_fields():
    conn = mock.MagicMock()
    profile = {
        "followersCount": "1234",
        "followingCount": "321",
        "displayName": "Decent Cloud",
        "bio": "p2p cloud",
    }
    with (
        mock.patch.object(rm, "kv_get", return_value=None),
        mock.patch.object(rm, "get_user_profile", return_value=profile),
        mock.patch.object(rm, "upsert_account") as upsert,
        mock.patch.object(rm, "kv_set") as kv_set,
        mock.patch.object(rm, "utc_now", return_value="2026-03-03T12:00:00+00:00"),
    ):
        out = rm.maybe_refresh_own_account_stats(conn)

    assert out is not None
    assert out["followerCount"] == 1234
    assert out["followingCount"] == 321
    upsert.assert_called_once_with(
        conn,
        username=rm.OUR_HANDLE,
        follower_count=1234,
        display_name="Decent Cloud",
        bio="p2p cloud",
        following_count=321,
    )
    kv_set.assert_called_once()


def test_refresh_own_account_stats_skips_when_recent():
    conn = mock.MagicMock()
    now = datetime.now(timezone.utc).isoformat()
    with (
        mock.patch.object(rm, "kv_get", return_value=now),
        mock.patch.object(rm, "get_user_profile") as get_profile,
        mock.patch.object(rm, "upsert_account") as upsert,
    ):
        out = rm.maybe_refresh_own_account_stats(conn)

    assert out is None
    get_profile.assert_not_called()
    upsert.assert_not_called()

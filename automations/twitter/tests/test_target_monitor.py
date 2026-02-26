"""Tests for twitter/target_monitor.py.

Tests cover:
- State loading/saving
- Peak hours detection
- New tweet detection logic (mocked CDP)
- Replied IDs deduplication and cap enforcement

Requires pytest. Run from /projects/automations/twitter/:
  uv run pytest tests/test_target_monitor.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Path setup and import stubs -- must happen before target_monitor is imported
# ---------------------------------------------------------------------------
sys.path.insert(0, "/projects/automations/twitter")
sys.path.insert(0, "/projects/automations")

# Stub heavy twitter_utils / lib imports so tests run without a live browser
_twitter_utils_stub = mock.MagicMock()
_twitter_utils_stub.BLOCKED_AUTHORS = []
_twitter_utils_stub.is_junk.return_value = False
sys.modules.setdefault("twitter_utils", _twitter_utils_stub)

_lib_llm_stub = mock.MagicMock()
sys.modules.setdefault("lib", mock.MagicMock())
sys.modules.setdefault("lib.llm_utils", _lib_llm_stub)
sys.modules.setdefault("lib.config", mock.MagicMock())

import importlib.util

TARGET_MONITOR_PATH = Path("/projects/automations/twitter/target_monitor.py")
spec = importlib.util.spec_from_file_location("target_monitor", TARGET_MONITOR_PATH)
tm = importlib.util.module_from_spec(spec)
tm.call_llm = _lib_llm_stub.call_llm_simple
tm.extract_json = _lib_llm_stub.extract_json
spec.loader.exec_module(tm)

# Patch module-level helpers that call external services
tm.is_junk = mock.MagicMock(return_value=False)
tm.lookup_our_thread = mock.MagicMock(return_value=None)
tm.load_project_context = mock.MagicMock(return_value="DecentCloud is a p2p cloud marketplace.")
tm.utc_now = mock.MagicMock(return_value="2026-02-22T15:00:00Z")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _real_now() -> datetime:
    """Return real UTC now -- needed so timestamp-age calculations are correct."""
    return datetime.now(timezone.utc)


def _dt_at(hour: int) -> datetime:
    """Return a fixed UTC datetime at the given hour (for peak-hours tests)."""
    return datetime(2026, 2, 22, hour, 0, tzinfo=timezone.utc)


def _make_state(**overrides) -> dict:
    base = {"accounts": {}, "repliedToIds": [], "lastRunAt": None}
    base.update(overrides)
    return base


def _fresh_tweet(tweet_id: str, age_min: float = 5.0) -> dict:
    """Create a fake CDP tweet result that is age_min minutes old relative to now."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    return {
        "tweetId": tweet_id,
        "text": "AWS egress fees are criminal",
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "href": f"https://x.com/simonw/status/{tweet_id}",
    }


def _tweet_ctx(tweet_id: str, author: str = "simonw") -> dict:
    return {
        "tweetId": tweet_id,
        "text": "AWS egress fees are criminal",
        "author": author,
        "authorName": "Simon Willison",
        "stats": {"likes": 42, "retweets": 5, "replies": 3},
        "parentChain": [],
        "otherReplies": [],
        "threadContinuation": None,
        "quotedTweet": None,
    }


# ---------------------------------------------------------------------------
# State loading / saving
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_load_monitor_state_missing_file(self, tmp_path, monkeypatch):
        """load_monitor_state returns empty defaults when state file is absent."""
        monkeypatch.setattr(tm, "STATE_PATH", tmp_path / "nonexistent.json")
        state = tm.load_monitor_state()
        assert state == {"accounts": {}, "repliedToIds": [], "lastRunAt": None}

    def test_load_monitor_state_valid_json(self, tmp_path, monkeypatch):
        """load_monitor_state correctly deserialises existing JSON."""
        state_file = tmp_path / "state.json"
        data = {
            "accounts": {"simonw": {"lastCheckedAt": "2026-02-22T10:00:00Z"}},
            "repliedToIds": ["111", "222"],
            "lastRunAt": "2026-02-22T10:00:00Z",
        }
        state_file.write_text(json.dumps(data))
        monkeypatch.setattr(tm, "STATE_PATH", state_file)

        loaded = tm.load_monitor_state()
        assert loaded["repliedToIds"] == ["111", "222"]
        assert "simonw" in loaded["accounts"]

    def test_load_monitor_state_corrupt_json(self, tmp_path, monkeypatch):
        """load_monitor_state returns defaults when the file contains garbage."""
        state_file = tmp_path / "state.json"
        state_file.write_text("not json at all {{{")
        monkeypatch.setattr(tm, "STATE_PATH", state_file)

        state = tm.load_monitor_state()
        assert state == {"accounts": {}, "repliedToIds": [], "lastRunAt": None}

    def test_save_monitor_state_roundtrip(self, tmp_path, monkeypatch):
        """save_monitor_state writes valid JSON that can be re-loaded."""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(tm, "STATE_PATH", state_file)

        tm.save_monitor_state(_make_state(repliedToIds=["abc123"]))

        assert state_file.exists()
        reloaded = json.loads(state_file.read_text())
        assert reloaded["repliedToIds"] == ["abc123"]

    def test_save_monitor_state_creates_parent_dirs(self, tmp_path, monkeypatch):
        """save_monitor_state creates missing parent directories."""
        deep_path = tmp_path / "a" / "b" / "c" / "state.json"
        monkeypatch.setattr(tm, "STATE_PATH", deep_path)

        tm.save_monitor_state(_make_state())
        assert deep_path.exists()

    def test_get_account_state_initialises_new_account(self):
        """get_account_state creates a fresh entry for unknown usernames."""
        state = _make_state()
        acct = tm.get_account_state(state, "newuser")
        assert acct == {"lastCheckedAt": None, "lastTweetId": None, "lastTweetAt": None}
        assert "newuser" in state["accounts"]

    def test_get_account_state_returns_existing(self):
        """get_account_state returns the existing dict if already present."""
        state = _make_state(
            accounts={"simonw": {"lastCheckedAt": "ts", "lastTweetId": "99", "lastTweetAt": None}}
        )
        acct = tm.get_account_state(state, "simonw")
        assert acct["lastTweetId"] == "99"


# ---------------------------------------------------------------------------
# Peak hours detection
# ---------------------------------------------------------------------------


class TestPeakHours:
    @pytest.mark.parametrize("hour,expected", [
        (13, True),   # 1pm UTC - peak starts
        (15, True),   # mid-afternoon UTC
        (20, True),   # evening UTC
        (23, True),   # late evening UTC
        (0,  True),   # midnight UTC - still peak
        (1,  True),   # 1am UTC - still peak
        (2,  True),   # 2am UTC - still peak
        (3,  False),  # 3am UTC - peak ended
        (5,  False),  # 5am UTC - off-peak
        (9,  False),  # 9am UTC - off-peak
        (12, False),  # noon UTC - just before peak
    ])
    def test_peak_hours_boundary(self, hour, expected):
        assert tm.is_peak_hours(_dt_at(hour)) is expected

    def test_peak_hours_uses_real_now_when_no_arg(self):
        """Calling is_peak_hours() with no args doesn't crash."""
        assert isinstance(tm.is_peak_hours(), bool)


# ---------------------------------------------------------------------------
# New tweet detection logic (mocked CDP)
# ---------------------------------------------------------------------------


class TestNewTweetDetection:
    def setup_method(self):
        """Reset is_junk mock to False before each test."""
        tm.is_junk.return_value = False

    def test_skips_same_tweet_id(self):
        """process_account returns False when tweet ID hasn't changed."""
        state = _make_state(accounts={"simonw": {
            "lastCheckedAt": None, "lastTweetId": "999", "lastTweetAt": None,
        }})
        with mock.patch.object(tm, "get_latest_profile_tweet",
                               return_value=_fresh_tweet("999", age_min=2)):
            result = tm.process_account("simonw", state, {}, set(), _real_now())
        assert result is False

    def test_skips_tweet_already_replied(self):
        """process_account skips a new tweet ID if it is in replied_ids."""
        state = _make_state(accounts={"simonw": {
            "lastCheckedAt": None, "lastTweetId": "100", "lastTweetAt": None,
        }})
        with mock.patch.object(tm, "get_latest_profile_tweet",
                               return_value=_fresh_tweet("777", age_min=2)):
            result = tm.process_account("simonw", state, {}, {"777"}, _real_now())
        assert result is False

    def test_skips_stale_tweet(self):
        """process_account returns False when tweet is older than MAX_TWEET_AGE_MIN."""
        state = _make_state(accounts={"simonw": {
            "lastCheckedAt": None, "lastTweetId": "100", "lastTweetAt": None,
        }})
        stale = _fresh_tweet("888", age_min=60)  # 60 min > MAX_TWEET_AGE_MIN=30
        with mock.patch.object(tm, "get_latest_profile_tweet", return_value=stale):
            result = tm.process_account("simonw", state, {}, set(), _real_now())
        assert result is False

    def test_skips_when_no_cdp_result(self):
        """process_account returns False when CDP returns None."""
        with mock.patch.object(tm, "get_latest_profile_tweet", return_value=None):
            result = tm.process_account("simonw", _make_state(), {}, set(), _real_now())
        assert result is False

    def test_skips_when_timestamp_unparseable(self):
        """process_account skips tweets with unparseable timestamps for safety."""
        state = _make_state(accounts={"simonw": {
            "lastCheckedAt": None, "lastTweetId": "old", "lastTweetAt": None,
        }})
        bad = {"tweetId": "new123", "text": "x", "timestamp": "not-a-real-ts", "href": "h"}
        with mock.patch.object(tm, "get_latest_profile_tweet", return_value=bad):
            result = tm.process_account("simonw", state, {}, set(), _real_now())
        assert result is False

    def test_skips_recently_checked_account(self):
        """process_account skips an account checked within MIN_CHECK_INTERVAL_MIN."""
        recent_check = (_real_now() - timedelta(minutes=5)).isoformat()
        state = _make_state(accounts={"simonw": {
            "lastCheckedAt": recent_check, "lastTweetId": None, "lastTweetAt": None,
        }})
        with mock.patch.object(tm, "get_latest_profile_tweet") as mock_cdp:
            result = tm.process_account("simonw", state, {}, set(), _real_now())
        assert result is False
        mock_cdp.assert_not_called()

    def test_skips_junk_tweet(self):
        """process_account skips tweets classified as junk."""
        state = _make_state(accounts={"simonw": {
            "lastCheckedAt": None, "lastTweetId": "old", "lastTweetAt": None,
        }})
        with (
            mock.patch.object(tm, "get_latest_profile_tweet",
                               return_value=_fresh_tweet("junk123", age_min=2)),
            mock.patch.object(tm, "is_junk", return_value=True),
        ):
            result = tm.process_account("simonw", state, {}, set(), _real_now())
        assert result is False

    def test_no_reply_when_llm_rejects(self):
        """process_account returns False when LLM says shouldEngage=False."""
        state = _make_state(accounts={"simonw": {
            "lastCheckedAt": None, "lastTweetId": "old", "lastTweetAt": None,
        }})
        ms = {"engagedPosts": [], "recentPosts": []}
        mon = dict(state); mon["_main_state"] = ms
        rejected = {
            "shouldEngage": False, "conversationLikelihood": 4,
            "reasoning": "Generic rant", "reply": None,
        }
        with (
            mock.patch.object(tm, "get_latest_profile_tweet",
                               return_value=_fresh_tweet("t456", age_min=5)),
            mock.patch.object(tm, "fetch_tweet_context", return_value=_tweet_ctx("t456")),
            mock.patch.object(tm, "get_user_profile", return_value=None),
            mock.patch.object(tm, "draft_target_reply", return_value=rejected),
            mock.patch.object(tm, "is_junk", return_value=False),
            mock.patch.object(tm, "post_reply") as mock_post,
        ):
            result = tm.process_account("simonw", mon, ms, set(), _real_now())
        assert result is False
        mock_post.assert_not_called()

    def test_full_happy_path_posts_reply(self):
        """process_account posts a reply for a fresh, relevant new tweet."""
        state = _make_state(accounts={"simonw": {
            "lastCheckedAt": None, "lastTweetId": "old_id", "lastTweetAt": None,
        }})
        ms = {"engagedPosts": [], "recentPosts": []}
        mon = dict(state); mon["_main_state"] = ms

        good_decision = {
            "shouldEngage": True,
            "conversationLikelihood": 8,
            "reasoning": "Strong cloud cost angle",
            "reply": "Egress is the hidden tax no one talks about until they get the bill.",
        }

        with (
            mock.patch.object(tm, "get_latest_profile_tweet",
                               return_value=_fresh_tweet("new_tweet", age_min=3)),
            mock.patch.object(tm, "fetch_tweet_context",
                               return_value=_tweet_ctx("new_tweet")),
            mock.patch.object(tm, "get_user_profile",
                               return_value={"followersCount": 50000}),
            mock.patch.object(tm, "draft_target_reply", return_value=good_decision),
            mock.patch.object(tm, "is_junk", return_value=False),
            mock.patch.object(tm, "humanize", side_effect=lambda x: x),
            mock.patch.object(tm, "post_reply", return_value=True),
            mock.patch.object(tm, "auto_follow_after_engagement"),
            mock.patch.object(tm, "log_recent"),
        ):
            result = tm.process_account("simonw", mon, ms, set(), _real_now())

        assert result is True
        assert "new_tweet" in mon.get("repliedToIds", [])
        assert mon["accounts"]["simonw"]["lastTweetId"] == "new_tweet"
        assert any(e.get("tweetId") == "new_tweet" for e in ms.get("engagedPosts", []))

    def test_post_reply_failure_does_not_record_id(self):
        """If post_reply fails, the tweet ID is NOT added to repliedToIds."""
        state = _make_state(accounts={"simonw": {
            "lastCheckedAt": None, "lastTweetId": "old_id", "lastTweetAt": None,
        }})
        ms = {"engagedPosts": [], "recentPosts": []}
        mon = dict(state); mon["_main_state"] = ms

        good_decision = {
            "shouldEngage": True, "conversationLikelihood": 8,
            "reasoning": "Good hook", "reply": "Egress is the hidden tax.",
        }

        with (
            mock.patch.object(tm, "get_latest_profile_tweet",
                               return_value=_fresh_tweet("fail_tweet", age_min=3)),
            mock.patch.object(tm, "fetch_tweet_context",
                               return_value=_tweet_ctx("fail_tweet")),
            mock.patch.object(tm, "get_user_profile", return_value=None),
            mock.patch.object(tm, "draft_target_reply", return_value=good_decision),
            mock.patch.object(tm, "is_junk", return_value=False),
            mock.patch.object(tm, "humanize", side_effect=lambda x: x),
            mock.patch.object(tm, "post_reply", return_value=False),
            mock.patch.object(tm, "send_error_alert"),
        ):
            result = tm.process_account("simonw", mon, ms, set(), _real_now())

        assert result is False
        assert "fail_tweet" not in mon.get("repliedToIds", [])


# ---------------------------------------------------------------------------
# Replied IDs deduplication and cap
# ---------------------------------------------------------------------------


class TestRepliedIdsCap:
    def test_add_replied_id_basic(self):
        state = _make_state()
        tm.add_replied_id(state, "tweet1")
        assert "tweet1" in state["repliedToIds"]

    def test_add_replied_id_no_duplicates(self):
        state = _make_state(repliedToIds=["tweet1"])
        tm.add_replied_id(state, "tweet1")
        assert state["repliedToIds"].count("tweet1") == 1

    def test_add_replied_id_cap_enforced(self):
        """add_replied_id drops oldest entries to stay within MAX_REPLIED_IDS."""
        ids = [str(i) for i in range(tm.MAX_REPLIED_IDS)]
        state = _make_state(repliedToIds=ids)

        tm.add_replied_id(state, "new_id")

        assert len(state["repliedToIds"]) == tm.MAX_REPLIED_IDS
        assert "new_id" in state["repliedToIds"]
        assert "0" not in state["repliedToIds"]  # oldest dropped

    def test_add_replied_id_coerces_to_string(self):
        state = _make_state()
        tm.add_replied_id(state, 123456)
        assert "123456" in state["repliedToIds"]


# ---------------------------------------------------------------------------
# parse_tweet_timestamp
# ---------------------------------------------------------------------------


class TestParseTimestamp:
    def test_valid_iso_with_z(self):
        ts = tm.parse_tweet_timestamp("2026-02-22T14:35:00.000Z")
        assert ts is not None
        assert ts.hour == 14
        assert ts.tzinfo is not None

    def test_valid_iso_with_offset(self):
        ts = tm.parse_tweet_timestamp("2026-02-22T14:35:00+00:00")
        assert ts is not None

    def test_none_input(self):
        assert tm.parse_tweet_timestamp(None) is None

    def test_empty_string(self):
        assert tm.parse_tweet_timestamp("") is None

    def test_garbage_string(self):
        assert tm.parse_tweet_timestamp("2pm yesterday") is None

    def test_naive_datetime_gets_utc(self):
        """A naive ISO string (no tz) gets UTC attached."""
        ts = tm.parse_tweet_timestamp("2026-02-22T14:35:00")
        assert ts is not None
        assert ts.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# TARGET_ACCOUNTS list sanity checks
# ---------------------------------------------------------------------------


class TestTargetAccountsList:
    def test_account_count(self):
        assert len(tm.TARGET_ACCOUNTS) >= 20

    def test_no_duplicates(self):
        lower = [a.lower() for a in tm.TARGET_ACCOUNTS]
        assert len(lower) == len(set(lower))

    def test_no_empty_strings(self):
        for acct in tm.TARGET_ACCOUNTS:
            assert acct.strip(), f"Empty entry in TARGET_ACCOUNTS: {acct!r}"

    def test_known_accounts_present(self):
        lower = {a.lower() for a in tm.TARGET_ACCOUNTS}
        for expected in ["kelseyhightower", "dhh", "simonw", "karpathy"]:
            assert expected in lower, f"{expected} missing from TARGET_ACCOUNTS"

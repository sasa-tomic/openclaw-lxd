"""Unit tests for fetch_tweet_context CDP logic.

Tests the retry / home-warmup path without a real browser by patching
cdp_tab.  No network required.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cdp_mock(*, first_wait_for: bool, second_wait_for: bool = True):
    """Return a CDPSession mock that simulates a specific wait_for pattern.

    first_wait_for  – result of the first wait_for call (after direct navigate)
    second_wait_for – result of the second wait_for call (after home warmup)
    """
    cdp = MagicMock()
    cdp.navigate.return_value = True

    # Fake tweet data returned by evaluate()
    fake_data = json.dumps({
        "username": "testauthor",
        "displayName": "Test Author",
        "text": "cloud support is terrible",
        "likes": 10,
        "retweets": 2,
        "replies": 3,
        "quotedTweet": None,
        "threadContinuation": None,
        "otherReplies": None,
        "parentChain": None,
    })
    cdp.evaluate.return_value = fake_data

    # wait_for returns first_wait_for on the 1st call, second_wait_for on the 2nd
    cdp.wait_for.side_effect = [first_wait_for, second_wait_for]

    # Support `with CDPSession.connect() as cdp:`
    cdp.__enter__ = lambda s: s
    cdp.__exit__ = MagicMock(return_value=False)
    return cdp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_happy_path_articles_found_immediately():
    """Articles appear on the first wait_for — no warmup needed."""
    cdp = _make_cdp_mock(first_wait_for=True)

    with patch("twitter_utils.cdp_tab") as mock_tab:
        mock_tab.return_value = contextlib.contextmanager(lambda: (yield cdp))()

        from twitter_utils import fetch_tweet_context
        result = fetch_tweet_context("1234567890")

    assert result is not None
    assert result["author"] == "testauthor"
    assert result["tweetId"] == "1234567890"
    assert result["text"] == "cloud support is terrible"
    assert result["stats"]["likes"] == 10

    nav_calls = [c.args[0] for c in cdp.navigate.call_args_list]
    assert any("1234567890" in u for u in nav_calls)
    assert not any("home" in u for u in nav_calls), "home warmup should NOT be triggered"


def test_stuck_tab_triggers_home_warmup():
    """First wait_for times out (stuck tab) → warmup via home → retry succeeds."""
    cdp = _make_cdp_mock(first_wait_for=False, second_wait_for=True)

    with patch("twitter_utils.cdp_tab") as mock_tab:
        mock_tab.return_value = contextlib.contextmanager(lambda: (yield cdp))()

        from twitter_utils import fetch_tweet_context
        result = fetch_tweet_context("1234567890")

    assert result is not None, "Should succeed after home warmup"
    assert result["author"] == "testauthor"

    nav_calls = [c.args[0] for c in cdp.navigate.call_args_list]
    assert any("home" in u for u in nav_calls), "Home warmup navigate must be called"
    assert nav_calls.index(next(u for u in nav_calls if "home" in u)) > 0, (
        "Home navigate must come after the first tweet navigate"
    )


def test_stuck_tab_warmup_also_fails_returns_none():
    """Both wait_for calls fail → returns None without crashing."""
    cdp = _make_cdp_mock(first_wait_for=False, second_wait_for=False)

    with patch("twitter_utils.cdp_tab") as mock_tab:
        mock_tab.return_value = contextlib.contextmanager(lambda: (yield cdp))()

        from twitter_utils import fetch_tweet_context
        result = fetch_tweet_context("1234567890")

    assert result is None, "Should return None when articles never appear"
    cdp.evaluate.assert_not_called()


def test_navigate_failure_returns_none():
    """navigate() returns False → returns None immediately, no wait_for called."""
    cdp = MagicMock()
    cdp.navigate.return_value = False

    with patch("twitter_utils.cdp_tab") as mock_tab:
        mock_tab.return_value = contextlib.contextmanager(lambda: (yield cdp))()

        from twitter_utils import fetch_tweet_context
        result = fetch_tweet_context("1234567890")

    assert result is None
    cdp.wait_for.assert_not_called()
    cdp.evaluate.assert_not_called()


def test_malformed_js_response_returns_none():
    """evaluate() returns garbage JSON → returns None without crashing."""
    cdp = _make_cdp_mock(first_wait_for=True)
    cdp.evaluate.return_value = "not json at all {{"

    with patch("twitter_utils.cdp_tab") as mock_tab:
        mock_tab.return_value = contextlib.contextmanager(lambda: (yield cdp))()

        from twitter_utils import fetch_tweet_context
        result = fetch_tweet_context("1234567890")

    assert result is None

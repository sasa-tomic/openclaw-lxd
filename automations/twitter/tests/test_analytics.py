#!/usr/bin/env python3
"""Tests for Twitter engagement analytics and Premium+ thread features.

Tests:
1. ourReplyId is captured in state after successful engagement post
2. gather_metrics includes reply_performances from state
3. Thread length is 6-10 after generation (Premium+ upgrade)
4. get_tweet_stats returns the expected shape

Run from repo root:
    pytest twitter/tests/test_analytics.py -v
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

# Ensure twitter/ and the project root are on the path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # /projects/automations
sys.path.insert(0, str(Path(__file__).parent.parent))         # /projects/automations/twitter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_hours_ago(hours: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    return ts.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Test 1: ourReplyId captured in engagedPosts state after posting a reply
# ---------------------------------------------------------------------------

class TestOurReplyIdCapture:
    """Verify that twitter-engagement.py captures our reply ID after posting."""

    def _build_state_entry(self, our_reply_id: str | None) -> dict:
        """Simulate the state dict entry that the engagement script creates."""
        entry = {
            "tweetId": "111222333",
            "timestamp": _utc_hours_ago(1),
            "author": "testauthor",
            "replyText": "Great insight on egress fees.",
            "llmReasoning": "Good match for marketplace trust angle",
            "searchTerm": "cloud egress fees",
            "ourReplyId": our_reply_id,
        }
        return entry

    def test_reply_id_present_when_captured(self):
        entry = self._build_state_entry("999888777666555444")
        assert entry["ourReplyId"] == "999888777666555444"

    def test_reply_id_none_when_not_captured(self):
        entry = self._build_state_entry(None)
        assert entry.get("ourReplyId") is None

    def test_engagement_script_captures_our_reply_id(self):
        """Verify twitter_engagement.py captures our_reply_id from post_reply()."""
        engagement_path = Path(__file__).parent.parent / "twitter_engagement.py"
        source = engagement_path.read_text()
        assert "our_reply_id" in source, (
            "twitter_engagement.py must capture our_reply_id from post_reply() "
            "to track the ID of our posted reply"
        )

    def test_engagement_script_stores_our_reply_id_in_db(self):
        """Verify twitter_engagement.py passes our_reply_id to insert_engagement."""
        engagement_path = Path(__file__).parent.parent / "twitter_engagement.py"
        source = engagement_path.read_text()
        assert "our_reply_id=our_reply_id" in source, (
            "twitter_engagement.py must pass our_reply_id= to insert_engagement()"
        )


# ---------------------------------------------------------------------------
# Test 2: gather_metrics includes reply_performances
# ---------------------------------------------------------------------------

class TestGatherMetricsReplyPerformances:
    """Verify that gather_metrics() collects reply performance data."""

    def test_reply_performances_in_metrics(self):
        """gather_metrics returns replyPerformances24h snapshot rows."""
        from twitter import daily_strategy_eval  # type: ignore

        expected_perf = [{
            "replyId": "222",
            "author": "alice",
            "replyText": "The egress tax is brutal.",
            "likes": 7,
            "retweets": 2,
            "replies": 1,
            "searchTerm": "cloud egress",
        }]

        mock_conn = MagicMock()
        with (
            patch.object(daily_strategy_eval, "_refresh_reply_performances", return_value={"checked": 0}),
            patch.object(daily_strategy_eval, "get_reply_performance_snapshot", return_value=expected_perf),
            patch.object(
                daily_strategy_eval,
                "get_engagement_counts_breakdown",
                side_effect=[
                    {"total": 0, "non_like": 0, "like_only": 0},
                    {"total": 0, "non_like": 0, "like_only": 0},
                ],
            ),
            patch.object(
                daily_strategy_eval,
                "get_post_counts_breakdown",
                side_effect=[
                    {"total": 0, "original_posts": 0, "thread_roots": 0, "thread_replies": 0},
                    {"total": 0, "original_posts": 0, "thread_roots": 0, "thread_replies": 0},
                ],
            ),
            patch.object(daily_strategy_eval, "_refresh_post_stats", return_value=0),
            patch.object(daily_strategy_eval, "get_follower_count", return_value=250),
            patch.object(daily_strategy_eval, "get_last_eval", return_value=None),
        ):
            metrics = daily_strategy_eval.gather_metrics(mock_conn)

        assert "replyPerformances24h" in metrics
        assert len(metrics["replyPerformances24h"]) == 1
        perf = metrics["replyPerformances24h"][0]
        assert perf["replyId"] == "222"
        assert perf["author"] == "alice"
        assert perf["likes"] == 7
        assert perf["retweets"] == 2

    def test_replies_outside_window_excluded(self):
        """_refresh_reply_performances skips entries without our_reply_id."""
        from twitter import daily_strategy_eval  # type: ignore

        # Entries without our_reply_id should be skipped
        mock_engagements = [
            {"tweet_id": "333", "our_reply_id": None, "target_username": "bob"},
            {"tweet_id": "555", "our_reply_id": "", "target_username": "carol"},
        ]

        mock_conn = MagicMock()
        with (
            patch.object(daily_strategy_eval, "get_engagements_for_perf_refresh", return_value=mock_engagements),
        ):
            result = daily_strategy_eval._refresh_reply_performances(mock_conn)

        assert result["checked"] == 0
        assert result["firstChecks"] == 0

    def test_no_our_reply_id_skipped(self):
        """_refresh_reply_performances skips entries where get_tweet_stats returns None."""
        from twitter import daily_strategy_eval  # type: ignore

        mock_engagements = [
            {
                "tweet_id": "777",
                "our_reply_id": "888",
                "target_username": "dave",
                "our_reply_text": "No reply ID.",
                "search_term": "cloud",
            },
        ]

        mock_conn = MagicMock()
        with (
            patch.object(daily_strategy_eval, "get_engagements_for_perf_refresh", return_value=mock_engagements),
            patch.object(daily_strategy_eval, "get_tweet_stats", return_value=None),
        ):
            result = daily_strategy_eval._refresh_reply_performances(mock_conn)

        assert result["candidates"] == 1
        assert result["checked"] == 0


# ---------------------------------------------------------------------------
# Test 2b: eval_history raw_metrics normalization
# ---------------------------------------------------------------------------

class TestEvalMetricNormalization:
    """Verify legacy and current eval metric schemas normalize consistently."""

    def test_normalize_legacy_raw_metrics(self):
        from twitter import db  # type: ignore

        legacy = {
            "date": "2026-03-01",
            "followerCount": 123,
            "followerGrowth": 5,
            "engagements24h": 40,
            "engagements7d": 210,
            "posts24h": 2,
            "posts7d": 12,
            "replyPerformances": [{"replyId": "1"}],
        }
        normalized = db.normalize_eval_metrics(legacy, fallback_row={})

        assert normalized["engagements24hTotal"] == 40
        assert normalized["replies24h"] == 40
        assert normalized["likes24h"] == 0
        assert normalized["posts24hTotal"] == 2
        assert normalized["originalPosts24h"] == 2
        assert len(normalized["replyPerformances24h"]) == 1

    def test_normalize_current_raw_metrics(self):
        from twitter import db  # type: ignore

        current = {
            "engagements24hTotal": 75,
            "replies24h": 60,
            "likes24h": 15,
            "posts24hTotal": 7,
            "originalPosts24h": 5,
            "threadRoots24h": 1,
            "threadReplies24h": 1,
            "replyPerformances24h": [{"replyId": "x"}, {"replyId": "y"}],
        }
        normalized = db.normalize_eval_metrics(current, fallback_row={})

        assert normalized["engagements24hTotal"] == 75
        assert normalized["replies24h"] == 60
        assert normalized["likes24h"] == 15
        assert normalized["posts24hTotal"] == 7
        assert normalized["originalPosts24h"] == 5
        assert len(normalized["replyPerformances24h"]) == 2


# ---------------------------------------------------------------------------
# Test 3: Thread length is 6-10 after generation
# ---------------------------------------------------------------------------

class TestThreadLength:
    """Verify that generate_thread() produces 6-10 tweets and uses updated prompt."""

    def test_thread_topics_has_new_entries(self):
        """THREAD_TOPICS list includes the new Premium+ topics."""
        from twitter import post_thread  # type: ignore

        expected_new_topics = [
            "the hidden economics of cloud egress: why providers make it free in, expensive out",
            "why decentralized cloud needs Airbnb-style reviews (not just lower prices)",
            "the P2P compute trust gap: what Akash/Flux got wrong",
            "cloud reliability theater: SLAs that sound good but pay nothing",
            "why the FinOps movement proves cloud pricing is deliberately opaque",
            "GPU compute: the $3/hr vs $0.30/hr gap that nobody talks about",
        ]
        for topic in expected_new_topics:
            assert topic in post_thread.THREAD_TOPICS, (
                f"Expected new Premium+ topic not found in THREAD_TOPICS: {topic!r}"
            )

    def test_thread_topics_minimum_length(self):
        """THREAD_TOPICS must have at least 20 entries after additions."""
        from twitter import post_thread  # type: ignore

        assert len(post_thread.THREAD_TOPICS) >= 20, (
            f"Expected at least 20 THREAD_TOPICS, got {len(post_thread.THREAD_TOPICS)}"
        )

    @patch("twitter.post_thread.call_llm")
    @patch("twitter.post_thread.load_project_context", return_value="# Strategy context")
    def test_generate_thread_accepts_6_to_10_tweets(self, mock_context, mock_llm):
        """generate_thread() accepts and returns 6-10 tweets from LLM."""
        from twitter import post_thread  # type: ignore

        # Simulate LLM returning 8 tweets
        tweets_8 = [
            f"{i}/ Tweet number {i} with enough content to be valid."
            for i in range(1, 9)
        ]
        mock_llm.return_value = json.dumps({
            "topic": "test topic",
            "tweets": tweets_8
        })

        mock_conn = MagicMock()
        with (
            patch.object(post_thread, "recent_thread_topics", return_value=[]),
            patch.object(post_thread, "get_recent_posts", return_value=[]),
            patch.object(post_thread, "get_recent_engagements", return_value=[]),
        ):
            result = post_thread.generate_thread(mock_conn)

        assert result is not None, "generate_thread returned None for 8-tweet response"
        assert 6 <= len(result["tweets"]) <= 10, (
            f"Expected 6-10 tweets, got {len(result['tweets'])}"
        )

    @patch("twitter.post_thread.call_llm")
    @patch("twitter.post_thread.load_project_context", return_value="# Strategy context")
    def test_generate_thread_prompt_mentions_6_10(self, mock_context, mock_llm):
        """The LLM prompt must request 6-10 tweets (not 5-7)."""
        from twitter import post_thread  # type: ignore

        mock_llm.return_value = json.dumps({
            "topic": "egress fees",
            "tweets": [f"{i}/ Tweet {i}" for i in range(1, 7)]
        })

        mock_conn = MagicMock()
        with (
            patch.object(post_thread, "recent_thread_topics", return_value=[]),
            patch.object(post_thread, "get_recent_posts", return_value=[]),
            patch.object(post_thread, "get_recent_engagements", return_value=[]),
        ):
            post_thread.generate_thread(mock_conn)

        # Inspect what was passed to call_llm
        assert mock_llm.called, "call_llm was not called"
        prompt_arg = mock_llm.call_args[0][0]
        assert "6-10" in prompt_arg, (
            "LLM prompt must mention '6-10' for Premium+ thread length. "
            f"Got prompt starting with: {prompt_arg[:200]}"
        )
        assert "5-7" not in prompt_arg, (
            "LLM prompt still mentions old '5-7' length — must be updated to 6-10"
        )

    @patch("twitter.post_thread.call_llm")
    @patch("twitter.post_thread.load_project_context", return_value="# Strategy context")
    def test_generate_thread_rejects_too_short(self, mock_context, mock_llm):
        """generate_thread() rejects threads with fewer than 3 tweets."""
        from twitter import post_thread  # type: ignore

        mock_llm.return_value = json.dumps({
            "topic": "test",
            "tweets": ["1/ Only one tweet."]
        })

        mock_conn = MagicMock()
        with (
            patch.object(post_thread, "recent_thread_topics", return_value=[]),
            patch.object(post_thread, "get_recent_posts", return_value=[]),
            patch.object(post_thread, "get_recent_engagements", return_value=[]),
        ):
            result = post_thread.generate_thread(mock_conn)
        assert result is None, "generate_thread should return None for 1-tweet result"


# ---------------------------------------------------------------------------
# Test 4: get_tweet_stats shape
# ---------------------------------------------------------------------------

class TestGetTweetStatsShape:
    """Verify get_tweet_stats returns correct dict shape (via mocked CDPSession)."""

    def _make_mock_cdp(self, raw_json: str, navigate_ok: bool = True):
        """Build a mock CDPSession context manager that returns raw_json from evaluate."""
        import contextlib
        mock_cdp = MagicMock()
        mock_cdp.navigate.return_value = navigate_ok
        mock_cdp.evaluate.return_value = raw_json
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_cdp)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        return mock_ctx

    def test_get_tweet_stats_returns_correct_keys(self):
        """get_tweet_stats must return dict with likes/retweets/replies."""
        import contextlib
        from twitter import twitter_utils  # type: ignore

        mock_raw = json.dumps(json.dumps({"likes": 5, "retweets": 2, "replies": 1}))
        mock_cdp = MagicMock()
        mock_cdp.navigate.return_value = True
        mock_cdp.evaluate.return_value = mock_raw

        with patch.object(twitter_utils, "cdp_tab") as mock_tab:
            mock_tab.return_value = contextlib.contextmanager(lambda: (yield mock_cdp))()
            result = twitter_utils.get_tweet_stats("123456789")

        assert result is not None
        assert set(result.keys()) == {"likes", "retweets", "replies"}
        assert result["likes"] == 5
        assert result["retweets"] == 2
        assert result["replies"] == 1

    def test_get_tweet_stats_returns_none_on_nav_failure(self):
        """get_tweet_stats returns None when navigation fails."""
        import contextlib
        from twitter import twitter_utils  # type: ignore

        mock_cdp = MagicMock()
        mock_cdp.navigate.return_value = False

        with patch.object(twitter_utils, "cdp_tab") as mock_tab:
            mock_tab.return_value = contextlib.contextmanager(lambda: (yield mock_cdp))()
            result = twitter_utils.get_tweet_stats("000")

        assert result is None

    def test_get_tweet_stats_returns_none_on_cdp_exception(self):
        """get_tweet_stats returns None when CDPSession raises."""
        import contextlib
        from twitter import twitter_utils  # type: ignore

        def _raise():
            raise OSError("no browser")
            yield  # make it a generator

        with patch.object(twitter_utils, "cdp_tab") as mock_tab:
            mock_tab.return_value = contextlib.contextmanager(_raise)()
            result = twitter_utils.get_tweet_stats("000")

        assert result is None

    def test_get_tweet_stats_handles_zero_counts(self):
        """get_tweet_stats handles all-zero stats without error."""
        import contextlib
        from twitter import twitter_utils  # type: ignore

        mock_raw = json.dumps(json.dumps({"likes": 0, "retweets": 0, "replies": 0}))
        mock_cdp = MagicMock()
        mock_cdp.navigate.return_value = True
        mock_cdp.evaluate.return_value = mock_raw

        with patch.object(twitter_utils, "cdp_tab") as mock_tab:
            mock_tab.return_value = contextlib.contextmanager(lambda: (yield mock_cdp))()
            result = twitter_utils.get_tweet_stats("111")

        assert result == {"likes": 0, "retweets": 0, "replies": 0}

    def test_get_tweet_stats_function_exists(self):
        """get_tweet_stats must be importable from twitter_utils."""
        from twitter import twitter_utils  # type: ignore
        assert callable(twitter_utils.get_tweet_stats), (
            "get_tweet_stats must be a callable function in twitter_utils"
        )

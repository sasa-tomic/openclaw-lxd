"""Deadlock-handling tests for daily_strategy_eval reply perf refresh."""

from __future__ import annotations

import sys
from unittest import mock

import psycopg2

sys.path.insert(0, "/projects/automations/twitter")

import daily_strategy_eval as de


def test_refresh_reply_performances_retries_term_upsert_on_deadlock():
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = mock.MagicMock()

    engagements = [
        {
            "tweet_id": "t1",
            "our_reply_id": "r1",
            "search_term": "aws outage",
            "perf_checked_at": None,
        }
    ]

    calls = {"n": 0}

    def flaky_term_upsert(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg2.errors.DeadlockDetected("deadlock")

    with (
        mock.patch.object(de, "get_engagements_for_perf_refresh", return_value=engagements),
        mock.patch.object(de, "get_tweet_stats", return_value={"likes": 2, "retweets": 0, "replies": 1}),
        mock.patch.object(de, "update_engagement_perf"),
        mock.patch.object(de, "update_search_term_perf", side_effect=flaky_term_upsert),
        mock.patch.object(de.time, "sleep"),
    ):
        out = de._refresh_reply_performances(conn)

    assert out["checked"] == 1
    assert out["firstChecks"] == 1
    assert calls["n"] == 2

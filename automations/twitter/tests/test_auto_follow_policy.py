"""Tests for source-aware auto-follow policy."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, "/projects/automations/twitter")
sys.path.insert(0, "/projects/automations")

import twitter_utils as tu


class _FakeCursor:
    def __init__(self, follows_today: int):
        self._follows_today = follows_today

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _query, _params=None):
        return None

    def fetchone(self):
        return [self._follows_today]


class _FakeConn:
    def __init__(self, follows_today: int):
        self._follows_today = follows_today

    def cursor(self):
        return _FakeCursor(self._follows_today)


def _fake_get_conn_factory(follows_today: int):
    @contextmanager
    def _fake_get_conn():
        yield _FakeConn(follows_today)

    return _fake_get_conn


def test_auto_follow_blocks_for_low_intent_source_without_relationship():
    with (
        mock.patch.object(tu, "get_conn", _fake_get_conn_factory(follows_today=0)),
        mock.patch.object(tu, "get_account", return_value={"stage": "candidate", "follows_us_back": False, "reply_back_count": 0}),
        mock.patch.object(tu, "follow_user") as follow_user,
    ):
        ok = tu.auto_follow_after_engagement(None, "someuser", "123", source="timeline")

    assert ok is False
    follow_user.assert_not_called()


def test_auto_follow_allows_direct_reply_source():
    with (
        mock.patch.object(tu, "get_conn", _fake_get_conn_factory(follows_today=0)),
        mock.patch.object(tu, "get_account", return_value={"stage": "candidate", "follows_us_back": False, "reply_back_count": 0}),
        mock.patch.object(tu, "is_followed", return_value=False),
        mock.patch.object(tu, "follow_user", return_value=True),
        mock.patch.object(tu, "upsert_account"),
        mock.patch.object(tu, "set_followed"),
    ):
        ok = tu.auto_follow_after_engagement(None, "anotheruser", "456", source="direct_reply")

    assert ok is True


def test_auto_follow_allows_warm_relationship_even_non_high_intent_source():
    with (
        mock.patch.object(tu, "get_conn", _fake_get_conn_factory(follows_today=0)),
        mock.patch.object(tu, "get_account", return_value={"stage": "warm", "follows_us_back": False, "reply_back_count": 0}),
        mock.patch.object(tu, "is_followed", return_value=False),
        mock.patch.object(tu, "follow_user", return_value=True),
        mock.patch.object(tu, "upsert_account") as upsert_account,
        mock.patch.object(tu, "set_followed") as set_followed,
    ):
        ok = tu.auto_follow_after_engagement(None, "freshuser", "789", source="search")

    assert ok is True
    upsert_account.assert_called_once()
    set_followed.assert_called_once()

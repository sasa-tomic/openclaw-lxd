"""Tests for account username normalization in db layer."""

from __future__ import annotations

import sys
from unittest import mock

sys.path.insert(0, "/projects/automations/twitter")

import db


def test_upsert_account_normalizes_username_to_lowercase():
    cur = mock.MagicMock()
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    db.upsert_account(conn, "DecentCloud_org", follower_count=10)

    assert cur.execute.called
    args, _kwargs = cur.execute.call_args
    params = args[1]
    assert params[0] == "decentcloud_org"

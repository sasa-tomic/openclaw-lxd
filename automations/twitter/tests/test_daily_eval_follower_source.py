"""Tests for daily_strategy_eval follower source of truth."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, "/projects/automations/twitter")
sys.path.insert(0, "/projects/automations")

import importlib.util

MODULE_PATH = Path("/projects/automations/twitter/daily_strategy_eval.py")
spec = importlib.util.spec_from_file_location("daily_strategy_eval", MODULE_PATH)
de = importlib.util.module_from_spec(spec)
spec.loader.exec_module(de)


def test_get_follower_count_reads_accounts_table_only():
    conn = mock.MagicMock()
    with mock.patch.object(de, "get_account", return_value={"follower_count": 42}):
        assert de.get_follower_count(conn) == 42

    with mock.patch.object(de, "get_account", return_value={"follower_count": None}):
        assert de.get_follower_count(conn) is None

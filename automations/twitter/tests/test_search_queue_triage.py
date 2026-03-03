from __future__ import annotations

import sys
from unittest import mock

sys.path.insert(0, "/projects/automations/twitter")

import search_queue as sq


def test_triage_for_queue_uses_ranked_ids_order():
    candidates = [
        {"tweet_id": "1", "text": "a", "likes": 1, "retweets": 0},
        {"tweet_id": "2", "text": "b", "likes": 2, "retweets": 0},
        {"tweet_id": "3", "text": "c", "likes": 3, "retweets": 0},
    ]
    with mock.patch.object(sq, "llm_triage_candidates", return_value=["2", "1"]):
        out = sq._triage_for_queue(candidates, top_n=2)
    assert [c["tweet_id"] for c in out] == ["2", "1"]


def test_triage_for_queue_empty_safe():
    assert sq._triage_for_queue([], top_n=10) == []


def test_default_invocation_sets_fill_defaults():
    with (
        mock.patch.object(sys, "argv", ["search_queue.py"]),
        mock.patch.object(sq, "cmd_fill", return_value=0) as mock_fill,
    ):
        rc = sq.main()
    assert rc == 0
    args = mock_fill.call_args[0][0]
    assert args.cmd == "fill"
    assert args.all is False
    assert args.n is None
    assert args.prepare is False
    assert args.triage_top == 20

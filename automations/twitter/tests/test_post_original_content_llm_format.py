from __future__ import annotations

import json
import sys
from unittest import mock

sys.path.insert(0, "/projects/automations/twitter")

import post_original_content as poc


def _patch_batch_dependencies():
    return mock.patch.multiple(
        poc,
        load_morning_research=mock.DEFAULT,
        get_recent_posts=mock.DEFAULT,
        get_recent_engagements=mock.DEFAULT,
        get_top_posts=mock.DEFAULT,
        get_popular_candidate_tweets=mock.DEFAULT,
        load_project_context=mock.DEFAULT,
        get_recent_commits=mock.DEFAULT,
    )


def test_llm_rank_candidates_accepts_strict_rankings_json():
    candidates = [
        {"text": "tweet one"},
        {"text": "tweet two"},
        {"text": "tweet three"},
    ]
    raw = json.dumps(
        {
            "rankings": [
                {"id": "C1", "score": 3, "reason": "ok"},
                {"id": "C2", "score": 9, "reason": "strong"},
                {"id": "C3", "score": 5, "reason": "fine"},
            ]
        }
    )
    with mock.patch.object(poc, "call_llm", return_value=raw):
        ranked = poc.llm_rank_candidates(candidates, top_posts=[])

    assert [c["text"] for c in ranked] == ["tweet two", "tweet three", "tweet one"]
    assert ranked[0]["llm_score"] == 9


def test_llm_rank_candidates_rejects_incomplete_rankings():
    candidates = [{"text": "one"}, {"text": "two"}]
    raw = json.dumps({"rankings": [{"id": "C1", "score": 9, "reason": "only one"}]})
    with mock.patch.object(poc, "call_llm", return_value=raw):
        ranked = poc.llm_rank_candidates(candidates, top_posts=[])

    assert ranked == candidates
    assert all("llm_score" not in c for c in ranked)


def test_draft_batch_rejects_plain_text_fallback():
    with _patch_batch_dependencies() as patched:
        patched["load_morning_research"].return_value = None
        patched["get_recent_posts"].return_value = []
        patched["get_recent_engagements"].return_value = []
        patched["get_top_posts"].return_value = []
        patched["get_popular_candidate_tweets"].return_value = []
        patched["load_project_context"].return_value = ""
        patched["get_recent_commits"].return_value = []
        with mock.patch.object(
            poc,
            "call_llm",
            return_value="Let me analyze this carefully:\n1) Here is a tweet idea",
        ):
            entries = poc.draft_batch(conn=object())

    assert entries == []


def test_draft_batch_accepts_json_array_only():
    tweets = [
        "Cloud bills rarely fail because of one big mistake; they fail because no one owns 17 tiny defaults.",
        "Most migration plans optimize for architecture diagrams, not pager load after month two.",
        "If your SRE team is the only team that can explain your retry strategy, you do not have reliability.",
        "Kubernetes debates are usually proxy wars for org design problems no one wants to name.",
    ]
    with _patch_batch_dependencies() as patched:
        patched["load_morning_research"].return_value = None
        patched["get_recent_posts"].return_value = []
        patched["get_recent_engagements"].return_value = []
        patched["get_top_posts"].return_value = []
        patched["get_popular_candidate_tweets"].return_value = []
        patched["load_project_context"].return_value = ""
        patched["get_recent_commits"].return_value = []
        with mock.patch.object(poc, "call_llm", return_value=json.dumps(tweets)):
            entries = poc.draft_batch(conn=object())

    assert len(entries) == 4
    assert all(isinstance(e.get("text"), str) and e["text"] for e in entries)


def test_draft_batch_rejects_meta_lines_inside_json_array():
    tweets = [
        "Let me analyze the requirements carefully before drafting.",
        "Most teams discover observability gaps only after the first major incident.",
    ]
    with _patch_batch_dependencies() as patched:
        patched["load_morning_research"].return_value = None
        patched["get_recent_posts"].return_value = []
        patched["get_recent_engagements"].return_value = []
        patched["get_top_posts"].return_value = []
        patched["get_popular_candidate_tweets"].return_value = []
        patched["load_project_context"].return_value = ""
        patched["get_recent_commits"].return_value = []
        with mock.patch.object(poc, "call_llm", return_value=json.dumps(tweets)):
            entries = poc.draft_batch(conn=object())

    assert len(entries) == 1
    assert "analyze the requirements" not in entries[0]["text"].lower()


def test_is_valid_candidate_text_rejects_prompt_like_text():
    assert not poc._is_valid_candidate_text("Generate 4 original posts for @DecentCloud_org")
    assert not poc._is_valid_candidate_text("Let me analyze the requirements carefully")
    assert not poc._is_valid_candidate_text("Each must be a DIFFERENT topic and angle.")
    assert not poc._is_valid_candidate_text("Avoid repeating recent post angles.")
    assert not poc._is_valid_candidate_text("Topics should align with the content mix.")
    assert not poc._is_valid_candidate_text("Recent posts to AVOID:")
    assert poc._is_valid_candidate_text(
        "Most teams over-optimize migration plans and under-invest in rollback drills."
    )

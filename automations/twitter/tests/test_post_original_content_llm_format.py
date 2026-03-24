from __future__ import annotations

import json
import sys
from unittest import mock

sys.path.insert(0, "/projects/automations/twitter")

import post_original_content as poc


def _patch_context_deps():
    """Patch the DB/context functions called by _build_context_sections."""
    return mock.patch.multiple(
        poc,
        get_latest_morning_research=mock.DEFAULT,
        get_recent_posts=mock.DEFAULT,
        get_top_posts=mock.DEFAULT,
        load_project_context=mock.DEFAULT,
        get_recent_commits=mock.DEFAULT,
    )


def _set_empty_context(patched):
    """Set all patched context deps to return empty/None."""
    patched["get_latest_morning_research"].return_value = None
    patched["get_recent_posts"].return_value = []
    patched["get_top_posts"].return_value = []
    patched["load_project_context"].return_value = ""
    patched["get_recent_commits"].return_value = []


def test_llm_rank_candidates_accepts_strict_rankings_json():
    candidates = [
        {"hook": "tweet one", "format": "cliffhanger", "reveal": "reveal one"},
        {"hook": "tweet two", "format": "deliberately_wrong", "reveal": "reveal two"},
        {"hook": "tweet three", "format": "confession", "reveal": "reveal three"},
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
        ranked = poc.llm_rank_candidates(candidates)

    assert [c["hook"] for c in ranked] == ["tweet two", "tweet three", "tweet one"]
    assert ranked[0]["llm_score"] == 9


def test_llm_rank_candidates_rejects_incomplete_rankings():
    candidates = [
        {"hook": "one", "format": "cliffhanger"},
        {"hook": "two", "format": "confession"},
    ]
    raw = json.dumps({"rankings": [{"id": "C1", "score": 9, "reason": "only one"}]})
    with mock.patch.object(poc, "call_llm", return_value=raw):
        ranked = poc.llm_rank_candidates(candidates)

    assert ranked == candidates
    assert all("llm_score" not in c for c in ranked)


def test_draft_batch_rejects_plain_text_fallback():
    with mock.patch.object(
        poc, "call_llm",
        return_value="Let me analyze this carefully:\n1) Here is a tweet idea",
    ):
        entries = poc.draft_batch(context="test context")

    assert entries == []


def test_draft_batch_accepts_thread_json_array():
    threads = [
        {
            "format": "cliffhanger",
            "hook": "Our team mass-migrated from AWS to GCP for the savings. Three months in we got the first real bill.",
            "reveal": "Egress fees alone were $14K/mo. They weren't on any pricing calculator. We migrated back within 6 weeks.",
        },
        {
            "format": "hot-take",
            "hook": "Kubernetes saves money. That's just a fact at this point.",
            "reveal": "Average K8s cluster runs at 13% utilization. The orchestrator itself eats 15-30% overhead.",
        },
        {
            "format": "question",
            "hook": "How many SaaS subscriptions are you paying for that you haven't opened in 6 months? Reply with your number. Mine is 11.",
            "reveal": "",
        },
        {
            "format": "story",
            "hook": "I've been running a production database without backups for 8 months. On purpose.",
            "reveal": "It's a 200MB SQLite file that rebuilds from an event log in 4 minutes.",
        },
    ]
    with mock.patch.object(poc, "call_llm", return_value=json.dumps(threads)):
        entries = poc.draft_batch(context="test context")

    assert len(entries) == 4
    assert all(e.get("thread_data") is not None for e in entries)
    assert entries[0]["thread_data"]["format"] == "cliffhanger"
    assert entries[2]["thread_data"]["format"] == "question"
    assert entries[2]["thread_data"]["reveal"] == ""


def test_draft_batch_rejects_meta_lines_inside_json_array():
    threads = [
        {
            "format": "cliffhanger",
            "hook": "Let me analyze the requirements carefully before drafting.",
            "reveal": "Some reveal text that won't matter.",
        },
        {
            "format": "story",
            "hook": "I accidentally deleted our entire S3 bucket during a Friday deploy. The backup script hadn't run in three weeks.",
            "reveal": "Total data loss: 2.4TB. Recovery cost: $47K in consultant fees. The intern who wrote the backup cron job had left 6 months ago.",
        },
    ]
    with mock.patch.object(poc, "call_llm", return_value=json.dumps(threads)):
        entries = poc.draft_batch(context="test context")

    assert len(entries) == 1
    assert "analyze the requirements" not in entries[0]["text"].lower()


def test_is_valid_tweet_text_rejects_prompt_like_text():
    assert not poc._is_valid_tweet_text("Generate 4 original posts for @DecentCloud_org")
    assert not poc._is_valid_tweet_text("Let me analyze the requirements carefully")
    assert not poc._is_valid_tweet_text("Recent posts to AVOID:")
    assert not poc._is_valid_tweet_text("Here are the guidelines for writing:")
    assert not poc._is_valid_tweet_text("Let me think about what angle to use.")
    assert poc._is_valid_tweet_text(
        "Most teams over-optimize migration plans and under-invest in rollback drills."
    )


def test_is_valid_thread_entry_validates_formats():
    # Valid thread with reveal
    assert poc._is_valid_thread_entry({
        "format": "story",
        "hook": "Our team mass-migrated from AWS to GCP for the savings. Three months in we got the first real bill.",
        "reveal": "Egress fees alone were $14K/mo. They weren't on any pricing calculator.",
    })

    # Valid standalone (no reveal)
    assert poc._is_valid_thread_entry({
        "format": "question",
        "hook": "How many SaaS subscriptions are you paying for that you haven't opened in 6 months? Reply with your number.",
        "reveal": "",
    })

    # Valid: any format can be standalone now
    assert poc._is_valid_thread_entry({
        "format": "observation",
        "hook": "Our team mass-migrated from AWS to GCP. Three months in we got the first real bill.",
        "reveal": "",
    })

    # Invalid: hook too short
    assert not poc._is_valid_thread_entry({
        "format": "story",
        "hook": "I broke prod.",
        "reveal": "It was bad.",
    })

    # Invalid: product mention
    assert not poc._is_valid_thread_entry({
        "format": "story",
        "hook": "We just launched Decent Cloud and it's better than everything else out there.",
        "reveal": "Check out our platform for the best cloud experience.",
    })


def test_parse_llm_json_handles_code_fences():
    raw = '```json\n[{"format": "cliffhanger", "hook": "test hook sentence here for validation.", "reveal": "test"}]\n```'
    result = poc._parse_llm_json(raw)
    assert isinstance(result, list)
    assert result[0]["format"] == "cliffhanger"


def test_clean_llm_text_strips_prefixes():
    assert poc._clean_llm_text('(Technical insight) Cloud is expensive.') == "Cloud is expensive."
    assert poc._clean_llm_text('"Quoted text here."') == "Quoted text here."
    assert poc._clean_llm_text("  Normal text.  ") == "Normal text."


def test_filter_valid_unposted_separates_thread_and_legacy():
    queue = [
        {"id": 1, "posted": False, "text": "Legacy tweet that is valid and complete.", "thread_data": None},
        {"id": 2, "posted": False, "text": "hook", "thread_data": {
            "format": "cliffhanger",
            "hook": "Our team migrated everything to the cloud for savings. Then the first quarterly bill arrived.",
            "reveal": "The egress fees alone were more than our entire on-prem infrastructure cost.",
        }},
        {"id": 3, "posted": True, "text": "already posted", "thread_data": None},
        {"id": 4, "posted": False, "text": "bad", "thread_data": None},  # too short
    ]
    valid = poc._filter_valid_unposted(queue)
    assert len(valid) == 2
    assert valid[0]["id"] == 1
    assert valid[1]["id"] == 2

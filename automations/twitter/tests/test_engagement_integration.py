#!/usr/bin/env python3
"""Integration test for the Twitter engagement pipeline.

Runs the FULL engagement pipeline — real DB, real CDP browser navigation,
real tweet scraping, real LLM analysis — but intercepts at the very last
step (post_reply) so nothing is actually submitted to Twitter.

What this covers:
  - DB candidate queue fetch
  - CDP navigation to each tweet URL
  - DOM scraping of tweet text, stats, parent chain
  - Author profile fetch (CDP navigation + scraping)
  - LLM analysis and reply drafting
  - humanize() pass on the draft
  - Correct DB inserts are prepared (but not executed — post_reply returns False)
  - 429 rate-limit errors from LLM don't crash the run

What is mocked:
  - post_reply()     → returns (False, None) — prevents actual submission
  - send_error_alert → suppressed (it fires whenever post_reply fails)
  - jitter_sleep     → no-op (speeds up the test)
  - auto_follow_after_engagement → no-op (follow calls need a real session)

Run from repo root:
    pytest twitter/tests/test_engagement_integration.py -v -s

This test is SLOW (2-5 min) because it does real CDP + LLM work.
Mark with -m integration if you want to skip in fast CI:
    pytest -m "not integration"
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_AUTOMATIONS_ROOT = str(Path(__file__).parent.parent.parent)
_TWITTER_ROOT = str(Path(__file__).parent.parent)
for _p in (_AUTOMATIONS_ROOT, _TWITTER_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_engagement():
    """Load twitter_engagement as a fresh module (avoids import-order issues)."""
    spec = importlib.util.spec_from_file_location(
        "_twitter_engagement_test",
        Path(_TWITTER_ROOT) / "twitter_engagement.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engagement():
    """Load the engagement module once for the whole test module."""
    return _load_engagement()


@pytest.fixture()
def captured_replies():
    """Collects what post_reply *would* have sent."""
    return []


# ---------------------------------------------------------------------------
# Integration test: full pipeline, real CDP + LLM, no actual submit
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_engagement_pipeline_dry_run(engagement, captured_replies):
    """Full pipeline: queue → CDP navigate → scrape → LLM → draft.

    post_reply is intercepted so nothing reaches Twitter.
    Everything else — browser navigation, DOM extraction, LLM calls — is real.
    Candidate limit is capped at 2 to keep the test fast.
    """
    def _mock_post_reply(tweet_id, reply_text):
        """Record what would have been posted, return failure so no DB insert."""
        captured_replies.append({
            "tweet_id": str(tweet_id),
            "reply": reply_text,
        })
        print(f"\n[DRY-RUN] Would have posted to {tweet_id}: {reply_text[:120]}", flush=True)
        return False, None  # prevents insert_engagement from running

    def _gqc_2(conn, limit=100):
        from db import get_queued_candidates as _real
        return _real(conn, limit=min(limit, 2))

    with (
        patch.object(engagement, "get_queued_candidates", _gqc_2),
        patch.object(engagement, "post_reply", _mock_post_reply),
        patch.object(engagement, "send_error_alert", lambda *a, **kw: None),
        patch.object(engagement, "jitter_sleep", lambda *a, **kw: None),
        patch.object(engagement, "auto_follow_after_engagement", lambda *a, **kw: None),
    ):
        rc = engagement.main()

    # ── Assertions ──────────────────────────────────────────────────────────

    assert rc == 0, f"main() returned non-zero exit code: {rc}"

    # Validate every reply the LLM would have sent
    for item in captured_replies:
        reply = item["reply"]
        tweet_id = item["tweet_id"]

        assert reply, f"Empty reply text for tweet {tweet_id}"
        assert len(reply) <= 280, (
            f"Reply too long ({len(reply)} chars) for tweet {tweet_id}: {reply!r}"
        )
        # Should not contain known AI tells
        bad_phrases = ["wild that", "funny how", "almost like", "turns out", "weird that"]
        for phrase in bad_phrases:
            assert phrase.lower() not in reply.lower(), (
                f"AI tell phrase {phrase!r} found in reply for {tweet_id}: {reply!r}"
            )
        # Should not contain product mentions (Phase 1 rule)
        assert "decent cloud" not in reply.lower(), (
            f"Product mention in reply for {tweet_id}: {reply!r}"
        )
        assert "sign up" not in reply.lower(), (
            f"CTA found in reply for {tweet_id}: {reply!r}"
        )

    print(f"\n[DRY-RUN] Pipeline complete. {len(captured_replies)} replies drafted, 0 posted.")


# ---------------------------------------------------------------------------
# Unit: LLM 429 exhaustion doesn't crash the run
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_llm_rate_limit_skips_gracefully(engagement):
    """When LLM exhausts all retries (429), the candidate is skipped — not a crash."""
    from db import get_conn, get_queued_candidates

    try:
        with get_conn() as conn:
            candidates = get_queued_candidates(conn, limit=2)
    except Exception as e:
        pytest.skip(f"DB not reachable: {e}")

    if not candidates:
        pytest.skip("No candidates in queue to test with")

    def _gqc_2(conn, limit=100):
        from db import get_queued_candidates as _real
        return _real(conn, limit=min(limit, 2))

    # Simulate LLM always returning None (as it does after 429 exhaustion)
    with (
        patch.object(engagement, "get_queued_candidates", _gqc_2),
        patch.object(engagement, "draft_reply_with_full_context", return_value=None),
        patch.object(engagement, "post_reply", return_value=(False, None)),
        patch.object(engagement, "send_error_alert", lambda *a, **kw: None),
        patch.object(engagement, "jitter_sleep", lambda *a, **kw: None),
        patch.object(engagement, "auto_follow_after_engagement", lambda *a, **kw: None),
        # Still do real CDP navigation for context fetch
    ):
        rc = engagement.main()

    assert rc == 0, "LLM 429 exhaustion must not crash the engagement run"


# ---------------------------------------------------------------------------
# Unit: post_reply failure triggers send_error_alert, no DB insert
# ---------------------------------------------------------------------------

def test_post_reply_failure_alerts_and_no_db_insert(engagement):
    """When post_reply returns False, send_error_alert is called and the
    engagement is NOT inserted into the DB."""
    alerts = []

    # Synthetic candidate + context that will always be "approved" by LLM
    fake_decision = {
        "shouldEngage": True,
        "conversationLikelihood": 9,
        "profileClickWorthy": True,
        "reasoning": "unit test",
        "reply": "Test reply text for the unit test.",
    }

    from db import get_conn, get_recent_engagements

    try:
        with get_conn() as conn:
            engagements_before = get_recent_engagements(conn, hours=1, limit=100)
            ids_before = {e["tweet_id"] for e in engagements_before}
    except Exception as e:
        pytest.skip(f"DB not reachable: {e}")

    # Use a fake tweet ID guaranteed not to be in the DB
    FAKE_TWEET_ID = "000000000000000001"

    with (
        patch.object(engagement, "get_queued_candidates", return_value=[{
            "tweetId": FAKE_TWEET_ID,
            "url": "https://x.com/test/status/000000000000000001",
            "author": "testuser",
            "text": "cloud costs are too high",
            "searchTerm": "cloud costs",
        }]),
        patch.object(engagement, "is_engaged", return_value=False),
        patch.object(engagement, "fetch_tweet_context", return_value={
            "tweetId": FAKE_TWEET_ID,
            "text": "cloud costs are too high",
            "author": "testuser",
            "stats": {"likes": 5, "retweets": 1, "replies": 2},
            "parentChain": [],
        }),
        patch.object(engagement, "get_user_profile", return_value=None),
        patch.object(engagement, "draft_reply_with_full_context", return_value=fake_decision),
        patch.object(engagement, "humanize", side_effect=lambda t: t),
        patch.object(engagement, "post_reply", return_value=(False, None)),
        patch.object(engagement, "send_error_alert", side_effect=lambda msg, **kw: alerts.append(msg)),
        patch.object(engagement, "jitter_sleep", lambda *a, **kw: None),
        patch.object(engagement, "auto_follow_after_engagement", lambda *a, **kw: None),

        patch.object(engagement, "mark_queue_processed", lambda *a, **kw: None),
    ):
        rc = engagement.main()

    assert rc == 0
    # send_error_alert must have been called for the failed post
    assert any(FAKE_TWEET_ID in a for a in alerts), (
        f"Expected send_error_alert to be called with tweet ID {FAKE_TWEET_ID}. "
        f"Alerts fired: {alerts}"
    )

    # The fake tweet should NOT be in the DB
    with get_conn() as conn:
        engagements_after = get_recent_engagements(conn, hours=1, limit=100)
        ids_after = {e["tweet_id"] for e in engagements_after}

    assert FAKE_TWEET_ID not in ids_after, (
        "Engagement was inserted into DB even though post_reply returned False"
    )


# ---------------------------------------------------------------------------
# Unit: reply is inserted into DB after a successful post
# ---------------------------------------------------------------------------

def test_successful_reply_inserted_into_db(engagement):
    """When post_reply returns True, insert_engagement must record it in the DB."""
    from db import get_conn, get_recent_engagements, get_engaged_tweet_ids

    try:
        with get_conn() as conn:
            pass  # check reachable
    except Exception as e:
        pytest.skip(f"DB not reachable: {e}")

    FAKE_TWEET_ID = "000000000000000002"
    FAKE_REPLY_ID = "000000000000000099"
    FAKE_REPLY_TEXT = "Nobody at AWS is losing sleep over your ticket."

    fake_decision = {
        "shouldEngage": True,
        "conversationLikelihood": 8,
        "profileClickWorthy": True,
        "reasoning": "unit test happy path",
        "reply": FAKE_REPLY_TEXT,
    }

    with (
        patch.object(engagement, "get_queued_candidates", return_value=[{
            "tweetId": FAKE_TWEET_ID,
            "url": "https://x.com/test/status/000000000000000002",
            "author": "testauthor",
            "text": "cloud support is terrible",
            "searchTerm": "cloud support terrible",
        }]),
        patch.object(engagement, "is_engaged", return_value=False),
        patch.object(engagement, "fetch_tweet_context", return_value={
            "tweetId": FAKE_TWEET_ID,
            "text": "cloud support is terrible",
            "author": "testauthor",
            "stats": {"likes": 10, "retweets": 2, "replies": 3},
            "parentChain": [],
        }),
        patch.object(engagement, "get_user_profile", return_value=None),
        patch.object(engagement, "draft_reply_with_full_context", return_value=fake_decision),
        patch.object(engagement, "humanize", side_effect=lambda t: t),
        patch.object(engagement, "post_reply", return_value=(True, FAKE_REPLY_ID)),
        patch.object(engagement, "send_error_alert", lambda *a, **kw: None),
        patch.object(engagement, "jitter_sleep", lambda *a, **kw: None),
        patch.object(engagement, "auto_follow_after_engagement", lambda *a, **kw: None),

        patch.object(engagement, "mark_queue_processed", lambda *a, **kw: None),
    ):
        rc = engagement.main()

    assert rc == 0

    # The engagement must now be in the DB
    with get_conn() as conn:
        engaged_ids = get_engaged_tweet_ids(conn)

    assert FAKE_TWEET_ID in engaged_ids, (
        f"Tweet {FAKE_TWEET_ID} not found in engaged_ids after successful post_reply"
    )

    # Clean up the test row
    try:
        from db import DATABASE_URL
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM engagements WHERE tweet_id = %s", (FAKE_TWEET_ID,))
        conn.commit()
        conn.close()
    except Exception:
        pass  # cleanup failure is non-fatal

"""PostgreSQL data layer for Twitter automation.

Each Prefect flow run is its own subprocess — no persistent connection pool needed.
Use get_conn() as a context manager for all DB access.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg2
import psycopg2.extras

def _load_db_url() -> str:
    url = os.environ.get("TWITTER_DB_URL", "")
    if url:
        return url
    # Fall back to the openclaw env file (set by systemd EnvironmentFile but not
    # available when scripts are run directly from the shell)
    env_file = os.path.expanduser("~/.openclaw/.env")
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TWITTER_DB_URL="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return url


DATABASE_URL = _load_db_url()

# ---------------------------------------------------------------------------
# Seed accounts inserted during ensure_schema()
# ---------------------------------------------------------------------------

SEED_ACCOUNTS = [
    {"username": "forrestbrazeal", "discovery_source": "seed", "relevance_notes": "cloud cost advocate"},
    {"username": "kelseyhightower", "discovery_source": "seed", "relevance_notes": "infra/k8s thought leader"},
    {"username": "dhh", "discovery_source": "seed", "relevance_notes": "cloud skeptic, self-hosting advocate"},
    {"username": "jessfraz", "discovery_source": "seed", "relevance_notes": "containers/infra practitioner"},
    {"username": "badamczewski_", "discovery_source": "seed", "relevance_notes": "cloud cost practitioner"},
    {"username": "QuinnyPig", "discovery_source": "seed", "relevance_notes": "AWS/cloud cost expert"},
    {"username": "iann_mcd", "discovery_source": "seed", "relevance_notes": "AWS product expert"},
    {"username": "ben11kehoe", "discovery_source": "seed", "relevance_notes": "serverless/AWS practitioner"},
    {"username": "swardley", "discovery_source": "seed", "relevance_notes": "wardley maps, cloud strategy"},
    {"username": "GergelyOrosz", "discovery_source": "seed", "relevance_notes": "infra/platform eng takes"},
    {"username": "mipsytipsy", "discovery_source": "seed", "relevance_notes": "Charity Majors - observability, Honeycomb CTO"},
    {"username": "copyconstruct", "discovery_source": "seed", "relevance_notes": "Cindy Sridharan - distributed systems"},
    {"username": "b0rk", "discovery_source": "seed", "relevance_notes": "Julia Evans - infra debugging, engaged voice"},
    {"username": "lizthegrey", "discovery_source": "seed", "relevance_notes": "SRE, on-call culture"},
    {"username": "adrianco", "discovery_source": "seed", "relevance_notes": "Netflix cloud origin, honest takes"},
    {"username": "simonw", "discovery_source": "seed", "relevance_notes": "Django creator, pragmatic infra"},
    {"username": "jeremydaly", "discovery_source": "seed", "relevance_notes": "serverless AWS, active in replies"},
    {"username": "PierreVincent", "discovery_source": "seed", "relevance_notes": "FinOps, cloud billing"},
    {"username": "levelsio", "discovery_source": "seed", "relevance_notes": "indie maker, self-hosting, cost-sensitive"},
    {"username": "karpathy", "discovery_source": "seed", "relevance_notes": "honest about GPU costs and infra"},
]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


@contextmanager
def get_conn():
    """Yield a psycopg2 connection that auto-commits or rolls back."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# KV state
# ---------------------------------------------------------------------------


def kv_get(conn, key: str) -> str | None:
    """Get a value from kv_state by key. Returns None if not found."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT value FROM kv_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else None


def kv_set(conn, key: str, value: str) -> None:
    """Insert or update a key-value pair in kv_state."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kv_state (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value,
                  updated_at = now()
            """,
            (key, value),
        )


def kv_get_json(conn, key: str, default):
    """Read JSON from kv_state, returning `default` on missing/parse error."""
    raw = kv_get(conn, key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def kv_set_json(conn, key: str, value) -> None:
    """Serialize value as JSON into kv_state."""
    kv_set(conn, key, json.dumps(value, ensure_ascii=False, default=str))


def kv_get_prefix(conn, prefix: str) -> dict[str, str]:
    """Return kv_state rows whose key starts with prefix."""
    like = f"{prefix}%"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT key, value
            FROM kv_state
            WHERE key LIKE %s
            """,
            (like,),
        )
        rows = cur.fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def upsert_account(
    conn,
    username: str,
    *,
    display_name: str | None = None,
    bio: str | None = None,
    follower_count: int | None = None,
    following_count: int | None = None,
    relevance_score: float | None = None,
    relevance_notes: str | None = None,
    discovery_source: str | None = None,
    discovered_via: str | None = None,
    stage: str | None = None,
    skip_reason: str | None = None,
    extra: dict | None = None,
) -> None:
    """Insert or update an account. Only updates non-None fields on conflict."""
    # Build INSERT columns and values
    cols = ["username"]
    vals: list[Any] = [username]

    if display_name is not None:
        cols.append("display_name")
        vals.append(display_name)
    if bio is not None:
        cols.append("bio")
        vals.append(bio)
    if follower_count is not None:
        cols.append("follower_count")
        vals.append(follower_count)
    if following_count is not None:
        cols.append("following_count")
        vals.append(following_count)
    if relevance_score is not None:
        cols.append("relevance_score")
        vals.append(relevance_score)
    if relevance_notes is not None:
        cols.append("relevance_notes")
        vals.append(relevance_notes)
    if discovery_source is not None:
        cols.append("discovery_source")
        vals.append(discovery_source)
    if discovered_via is not None:
        cols.append("discovered_via")
        vals.append(discovered_via)
    if stage is not None:
        cols.append("stage")
        vals.append(stage)
    if skip_reason is not None:
        cols.append("skip_reason")
        vals.append(skip_reason)
    if extra is not None:
        cols.append("extra")
        vals.append(json.dumps(extra))

    col_str = ", ".join(cols)
    placeholder_str = ", ".join(["%s"] * len(cols))

    # Build UPDATE SET clause — only update fields that were explicitly passed
    update_parts = []
    for col in cols:
        if col == "username":
            continue
        if col == "extra":
            update_parts.append(f"{col} = EXCLUDED.{col}")
        else:
            update_parts.append(f"{col} = EXCLUDED.{col}")

    if not update_parts:
        # Nothing to update — just ensure the row exists
        sql = f"""
            INSERT INTO accounts ({col_str})
            VALUES ({placeholder_str})
            ON CONFLICT (username) DO NOTHING
        """
    else:
        update_str = ", ".join(update_parts)
        sql = f"""
            INSERT INTO accounts ({col_str})
            VALUES ({placeholder_str})
            ON CONFLICT (username) DO UPDATE SET {update_str}
        """

    with conn.cursor() as cur:
        cur.execute(sql, vals)


def get_account(conn, username: str) -> dict | None:
    """Fetch a single account by username. Returns None if not found."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM accounts WHERE username = %s", (username,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_account_stage(conn, username: str, stage: str) -> None:
    """Update the stage of an account."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET stage = %s WHERE username = %s",
            (stage, username),
        )


def set_followed(conn, username: str) -> None:
    """Mark an account as followed (stage='followed', followed_at=now())."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts
            SET stage = 'followed', followed_at = now()
            WHERE username = %s
            """,
            (username,),
        )


def set_follows_us_back(conn, username: str, follows: bool) -> None:
    """Update follows_us_back and follows_checked_at for an account."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts
            SET follows_us_back = %s, follows_checked_at = now()
            WHERE username = %s
            """,
            (follows, username),
        )


def increment_engagement_count(conn, username: str) -> None:
    """Increment engagement_count and set last_engaged_at for an account."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts
            SET engagement_count = engagement_count + 1, last_engaged_at = now()
            WHERE username = %s
            """,
            (username,),
        )


def increment_reply_back_count(conn, username: str) -> None:
    """Increment reply_back_count for an account."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET reply_back_count = reply_back_count + 1 WHERE username = %s",
            (username,),
        )


def get_accounts_by_stage(conn, stage: str, limit: int = 100) -> list[dict]:
    """Fetch accounts by stage, ordered by relevance_score DESC."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM accounts
            WHERE stage = %s
            ORDER BY relevance_score DESC NULLS LAST, discovered_at ASC
            LIMIT %s
            """,
            (stage, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def get_top_scored_candidates(conn, limit: int = 30) -> list[dict]:
    """Fetch top-scored candidates ready to be followed."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM accounts
            WHERE stage = 'scored'
            ORDER BY relevance_score DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def is_followed(conn, username: str) -> bool:
    """Return True if this account is already followed (stage in followed/engaged/warm/follower)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM accounts
            WHERE username = %s
              AND stage IN ('followed', 'engaged', 'warm', 'follower')
            LIMIT 1
            """,
            (username,),
        )
        return cur.fetchone() is not None


def get_all_followed_usernames(conn) -> set[str]:
    """Return set of all usernames with stage in ('followed', 'engaged', 'warm', 'follower')."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT username FROM accounts
            WHERE stage IN ('followed', 'engaged', 'warm', 'follower')
            """
        )
        return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Social edges
# ---------------------------------------------------------------------------


def upsert_edge(conn, source: str, target: str, type: str) -> None:
    """Insert a social edge, updating seen_at on conflict."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO social_edges (source, target, type, seen_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (source, target, type) DO UPDATE SET seen_at = now()
            """,
            (source, target, type),
        )


def get_reply_targets(conn, source: str) -> list[str]:
    """Return list of usernames that source has replied to."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT target FROM social_edges
            WHERE source = %s AND type = 'replies_to'
            ORDER BY seen_at DESC
            """,
            (source,),
        )
        return [row[0] for row in cur.fetchall()]


def get_candidates_from_edges(conn, sources: list[str], limit: int = 200) -> list[str]:
    """Find unique reply targets from these sources that aren't in accounts table yet."""
    if not sources:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT se.target
            FROM social_edges se
            WHERE se.source = ANY(%s)
              AND se.type = 'replies_to'
              AND se.target NOT IN (SELECT username FROM accounts)
            LIMIT %s
            """,
            (sources, limit),
        )
        return [row[0] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Engagements
# ---------------------------------------------------------------------------


def insert_engagement(
    conn,
    tweet_id: str,
    target_username: str,
    our_reply_text: str,
    our_reply_id: str | None = None,
    source: str | None = None,
    search_term: str | None = None,
    conv_likelihood: int | None = None,
    profile_click_worthy: bool | None = None,
    llm_reasoning: str | None = None,
    target_tweet_text: str | None = None,
    tweet_url: str | None = None,
    tweet_likes: int | None = None,
    tweet_rts: int | None = None,
    tweet_replies: int | None = None,
) -> None:
    """Insert an engagement record. Does nothing on duplicate tweet_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO engagements (
                tweet_id, target_username, target_tweet_text, tweet_url,
                tweet_likes, tweet_rts, tweet_replies,
                our_reply_text, our_reply_id,
                replied_at, source, search_term, conv_likelihood,
                profile_click_worthy, llm_reasoning
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s)
            ON CONFLICT (tweet_id) DO NOTHING
            """,
            (
                tweet_id,
                target_username,
                target_tweet_text,
                tweet_url,
                tweet_likes,
                tweet_rts,
                tweet_replies,
                our_reply_text,
                our_reply_id,
                source,
                search_term,
                conv_likelihood,
                profile_click_worthy,
                llm_reasoning,
            ),
        )


def is_engaged(conn, tweet_id: str) -> bool:
    """Return True if we already have an engagement record for this tweet_id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM engagements WHERE tweet_id = %s LIMIT 1",
            (tweet_id,),
        )
        return cur.fetchone() is not None


def get_recent_engagements(conn, hours: int = 24, limit: int = 100) -> list[dict]:
    """Fetch recent engagements within the last N hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM engagements
            WHERE replied_at >= %s
            ORDER BY replied_at DESC
            LIMIT %s
            """,
            (since, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def get_engagements_with_user(conn, username: str, limit: int = 10) -> list[dict]:
    """Fetch our most recent engagements with a specific user (DB only, no API calls).

    Returns list of dicts with: tweet_id, target_tweet_text, our_reply_text,
    replied_at, got_reply_back — newest first.
    Uses the existing idx_eng_user index.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT tweet_id, target_tweet_text, our_reply_text, replied_at, got_reply_back
            FROM engagements
            WHERE LOWER(target_username) = LOWER(%s)
            ORDER BY replied_at DESC
            LIMIT %s
            """,
            (username, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def get_engagements_for_perf_refresh(conn) -> list[dict]:
    """Fetch engagements that need a performance stats refresh.

    Covers:
    - First check: any reply 2h–7 days old that has never been perf-checked.
    - Re-checks: any reply < 7 days old whose stats were last fetched > 23h ago,
      so daily_strategy_eval keeps the numbers fresh throughout the engagement's
      lifecycle (not just a single one-shot snapshot in the first 24h).
    """
    now = datetime.now(timezone.utc)
    two_hours_ago = now - timedelta(hours=2)
    seven_days_ago = now - timedelta(days=7)
    twenty_three_hours_ago = now - timedelta(hours=23)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM engagements
            WHERE replied_at BETWEEN %s AND %s
              AND our_reply_id IS NOT NULL
              AND (perf_checked_at IS NULL OR perf_checked_at < %s)
            ORDER BY replied_at DESC
            """,
            (seven_days_ago, two_hours_ago, twenty_three_hours_ago),
        )
        return [dict(row) for row in cur.fetchall()]


def update_engagement_perf(
    conn,
    tweet_id: str,
    likes: int,
    rts: int,
    replies: int,
    got_reply_back: bool,
) -> None:
    """Update performance metrics on an engagement record."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE engagements
            SET reply_likes = %s,
                reply_rts = %s,
                reply_replies = %s,
                got_reply_back = %s,
                perf_checked_at = now()
            WHERE tweet_id = %s
            """,
            (likes, rts, replies, got_reply_back, tweet_id),
        )


def get_engaged_tweet_ids(conn) -> set[str]:
    """Return the set of all tweet IDs we have engagements for."""
    with conn.cursor() as cur:
        cur.execute("SELECT tweet_id FROM engagements")
        return {row[0] for row in cur.fetchall()}


def get_engagement_counts_breakdown(conn, since: datetime) -> dict[str, int]:
    """Return engagement counts since `since`, split by non-like vs like actions."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                COUNT(*)::int AS total,
                COUNT(*) FILTER (WHERE COALESCE(source, '') <> 'like')::int AS non_like,
                COUNT(*) FILTER (WHERE source = 'like')::int AS like_only
            FROM engagements
            WHERE replied_at >= %s
            """,
            (since,),
        )
        row = cur.fetchone() or {}
        return {
            "total": int(row.get("total", 0) or 0),
            "non_like": int(row.get("non_like", 0) or 0),
            "like_only": int(row.get("like_only", 0) or 0),
        }


def get_reply_performance_snapshot(conn, since: datetime, limit: int = 500) -> list[dict]:
    """Return reply performance rows for a true time window snapshot.

    Includes only actual replies/quotes (excludes source='like') with captured
    our_reply_id. Uses persisted engagement stats fields already kept up to date
    by daily_strategy_eval refresh.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                our_reply_id,
                target_username,
                our_reply_text,
                reply_likes,
                reply_rts,
                reply_replies,
                search_term,
                perf_checked_at,
                replied_at
            FROM engagements
            WHERE replied_at >= %s
              AND our_reply_id IS NOT NULL
              AND COALESCE(source, '') <> 'like'
            ORDER BY replied_at DESC
            LIMIT %s
            """,
            (since, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "replyId": row["our_reply_id"],
            "author": row["target_username"],
            "replyText": (row.get("our_reply_text") or "")[:100],
            "likes": int(row.get("reply_likes") or 0),
            "retweets": int(row.get("reply_rts") or 0),
            "replies": int(row.get("reply_replies") or 0),
            "searchTerm": row.get("search_term"),
            "repliedAt": (
                row["replied_at"].isoformat()
                if isinstance(row.get("replied_at"), datetime)
                else str(row.get("replied_at") or "")
            ),
            "perfCheckedAt": (
                row["perf_checked_at"].isoformat()
                if isinstance(row.get("perf_checked_at"), datetime)
                else (
                    str(row.get("perf_checked_at"))
                    if row.get("perf_checked_at") is not None
                    else None
                )
            ),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


def insert_post(
    conn,
    tweet_id: str,
    type: str,
    text: str,
    url: str | None = None,
) -> None:
    """Insert a post record. Does nothing on duplicate tweet_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO posts (tweet_id, posted_at, type, text, url)
            VALUES (%s, now(), %s, %s, %s)
            ON CONFLICT (tweet_id) DO NOTHING
            """,
            (tweet_id, type, text, url),
        )


def get_top_posts(conn, limit: int = 20) -> list[dict]:
    """Fetch top-performing posts ordered by total engagement (likes + rts)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT text, likes, rts, posted_at
            FROM posts
            WHERE text IS NOT NULL
            ORDER BY (COALESCE(likes, 0) + COALESCE(rts, 0)) DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def get_top_reply_combos(conn, limit: int = 20) -> list[dict]:
    """Fetch top-performing (tweet → our reply) pairs ranked by reply engagement.

    Used as few-shot style anchors in the reply drafting prompt — the LLM copies
    what already worked rather than following abstract rules.

    Only returns rows where we have actual positive engagement (reply_likes > 0)
    and both tweet text and our reply text are stored.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT target_tweet_text,
                   our_reply_text,
                   target_username,
                   reply_likes,
                   reply_rts,
                   got_reply_back
            FROM engagements
            WHERE reply_likes > 0
              AND our_reply_text IS NOT NULL
              AND target_tweet_text IS NOT NULL
            ORDER BY (reply_likes + COALESCE(reply_rts, 0)) DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def get_bottom_reply_combos(conn, limit: int = 5) -> list[dict]:
    """Fetch zero-engagement (tweet → our reply) pairs as negative examples.

    Random sample of perf-checked replies that got no likes, no RTs, and no
    reply back — shown alongside top combos so the LLM can see what falls flat.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT target_tweet_text,
                   our_reply_text,
                   target_username,
                   reply_likes,
                   reply_rts,
                   got_reply_back
            FROM engagements
            WHERE perf_checked_at IS NOT NULL
              AND (reply_likes IS NULL OR reply_likes = 0)
              AND (reply_rts IS NULL OR reply_rts = 0)
              AND got_reply_back = FALSE
              AND our_reply_text IS NOT NULL
              AND target_tweet_text IS NOT NULL
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def get_our_thread_context(conn, tweet_ids: list[str]) -> str | None:
    """Return our own post texts if any tweet_id matches posts we've made.

    Used by engagement monitors to give the LLM context when someone replies to
    one of our threads.  Returns a newline-separated string ordered by post time,
    or None if none of the IDs match our posts.
    """
    if not tweet_ids:
        return None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT tweet_id, text, posted_at
            FROM posts
            WHERE tweet_id = ANY(%s)
            ORDER BY posted_at ASC
            """,
            (tweet_ids,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    lines = [f"Tweet {i + 1}: {row['text']}" for i, row in enumerate(rows)]
    return "\n".join(lines)


def get_popular_candidate_tweets(conn, days: int = 30, limit: int = 25) -> list[dict]:
    """Fetch high-engagement tweets from candidate_queue as topic inspiration.

    These are tweets by others in our target space that got real traction —
    useful as angle/topic inspiration when drafting original posts.
    Only returns tweets with at least 1 like to filter noise.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT author, text, search_term, likes, retweets, replies, tweet_datetime
            FROM candidate_queue
            WHERE tweet_datetime >= %s
              AND likes > 0
              AND text IS NOT NULL
            ORDER BY (likes + retweets) DESC
            LIMIT %s
            """,
            (cutoff, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def upsert_tweet_replies(conn, parent_tweet_id: str, replies: list[dict]) -> int:
    """Persist reply stats for a tweet we fetched context for.

    Only inserts/updates replies that have a valid tweetId.
    Uses ON CONFLICT to refresh stats if we re-scrape the same reply.
    Returns count of rows upserted.
    """
    if not replies:
        return 0
    upserted = 0
    for r in replies:
        reply_id = r.get("tweetId")
        if not reply_id:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tweet_replies
                    (reply_tweet_id, parent_tweet_id, author, text, likes, retweets, replies, scraped_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (reply_tweet_id) DO UPDATE SET
                    likes      = EXCLUDED.likes,
                    retweets   = EXCLUDED.retweets,
                    replies    = EXCLUDED.replies,
                    scraped_at = now()
                """,
                (
                    reply_id,
                    parent_tweet_id,
                    r.get("username"),
                    (r.get("text") or "")[:500],
                    r.get("likes", 0),
                    r.get("retweets", 0),
                    r.get("replies", 0),
                ),
            )
        upserted += 1
    return upserted


def get_top_tweet_replies(conn, days: int = 30, min_engagement: int = 1, limit: int = 50) -> list[dict]:
    """Fetch top-engaged replies we've seen across all candidate tweets.

    Useful for pattern mining: what reply styles/angles get traction in our target space.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT reply_tweet_id, parent_tweet_id, author, text,
                   likes, retweets, replies, scraped_at
            FROM tweet_replies
            WHERE scraped_at >= %s
              AND (likes + retweets) >= %s
            ORDER BY (likes + retweets) DESC
            LIMIT %s
            """,
            (cutoff, min_engagement, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def get_posts_for_stats_update(conn, days: int = 30) -> list[dict]:
    """Return posts that need a stats refresh.

    Covers posts up to `days` old that are at least 2h old (enough time for
    the tweet to appear in the API) and whose stats were never fetched or were
    last fetched more than 23h ago (so daily_strategy_eval keeps them current).
    Excludes placeholder tweet_ids written when the real ID wasn't available.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    two_hours_ago = now - timedelta(hours=2)
    twenty_three_hours_ago = now - timedelta(hours=23)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT tweet_id, text, likes, rts, posted_at
            FROM posts
            WHERE posted_at BETWEEN %s AND %s
              AND tweet_id NOT LIKE 'unknown-%%'
              AND (perf_checked_at IS NULL OR perf_checked_at < %s)
            ORDER BY posted_at DESC
            """,
            (cutoff, two_hours_ago, twenty_three_hours_ago),
        )
        return [dict(row) for row in cur.fetchall()]


def update_post_stats(conn, tweet_id: str, likes: int, rts: int) -> None:
    """Update likes/rts for an original post and stamp perf_checked_at."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE posts
            SET likes = %s,
                rts = %s,
                perf_checked_at = now()
            WHERE tweet_id = %s
            """,
            (likes, rts, tweet_id),
        )


def get_recent_posts(conn, days: int = 7, limit: int = 50) -> list[dict]:
    """Fetch posts from the last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM posts
            WHERE posted_at >= %s
            ORDER BY posted_at DESC
            LIMIT %s
            """,
            (since, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def count_posts_today(conn) -> int:
    """Count posts made since midnight UTC today."""
    today_midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM posts WHERE posted_at >= %s",
            (today_midnight,),
        )
        row = cur.fetchone()
        return row[0] if row else 0


def get_post_counts_breakdown(conn, since: datetime) -> dict[str, int]:
    """Return post counts since `since`, split by original post vs thread items."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                COUNT(*)::int AS total,
                COUNT(*) FILTER (WHERE type = 'post')::int AS original_posts,
                COUNT(*) FILTER (WHERE type = 'thread')::int AS thread_roots,
                COUNT(*) FILTER (WHERE type = 'thread_reply')::int AS thread_replies
            FROM posts
            WHERE posted_at >= %s
            """,
            (since,),
        )
        row = cur.fetchone() or {}
        return {
            "total": int(row.get("total", 0) or 0),
            "original_posts": int(row.get("original_posts", 0) or 0),
            "thread_roots": int(row.get("thread_roots", 0) or 0),
            "thread_replies": int(row.get("thread_replies", 0) or 0),
        }


# ---------------------------------------------------------------------------
# Search term stats
# ---------------------------------------------------------------------------


def get_search_term_stats(conn) -> dict[str, dict]:
    """Return search term stats as a dict matching the old state["searchTermStats"] format.

    Format: {term: {"candidates": N, "engaged": N, "perfMeasured": N,
                     "totalLikes": N, "totalReplies": N, "zeroPerfReplies": N}}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM search_term_stats")
        rows = cur.fetchall()

    result: dict[str, dict] = {}
    for row in rows:
        result[row["term"]] = {
            "candidates": row["candidates_seen"] or 0,
            "engaged": row["engaged"] or 0,
            "perfMeasured": row["perf_measured"] or 0,
            "totalLikes": row["total_likes"] or 0,
            "totalReplies": row["total_reply_backs"] or 0,
            "zeroPerfReplies": row["zero_perf_replies"] or 0,
        }
    return result


def upsert_search_term_stats(
    conn,
    term: str,
    *,
    candidates_delta: int = 0,
    engaged_delta: int = 0,
) -> None:
    """Increment candidate and engaged counters for a search term."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO search_term_stats (term, candidates_seen, engaged, last_used_at, updated_at)
            VALUES (%s, %s, %s, now(), now())
            ON CONFLICT (term) DO UPDATE SET
                candidates_seen = search_term_stats.candidates_seen + EXCLUDED.candidates_seen,
                engaged = search_term_stats.engaged + EXCLUDED.engaged,
                last_used_at = now(),
                updated_at = now()
            """,
            (term, candidates_delta, engaged_delta),
        )


def update_search_term_perf(
    conn, term: str, likes: int, reply_backs: int, zero_perf: int
) -> None:
    """Update performance counters for a search term."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO search_term_stats (term, perf_measured, total_likes, total_reply_backs, zero_perf_replies, updated_at)
            VALUES (%s, 1, %s, %s, %s, now())
            ON CONFLICT (term) DO UPDATE SET
                perf_measured = search_term_stats.perf_measured + 1,
                total_likes = search_term_stats.total_likes + EXCLUDED.total_likes,
                total_reply_backs = search_term_stats.total_reply_backs + EXCLUDED.total_reply_backs,
                zero_perf_replies = search_term_stats.zero_perf_replies + EXCLUDED.zero_perf_replies,
                updated_at = now()
            """,
            (term, likes, reply_backs, zero_perf),
        )


# ---------------------------------------------------------------------------
# Eval history
# ---------------------------------------------------------------------------


def normalize_eval_metrics(raw_metrics: dict | None, fallback_row: dict | None = None) -> dict:
    """Return a canonical eval metrics payload across legacy and current schemas.

    Legacy rows used keys like engagements24h/posts24h/replyPerformances.
    Current rows use explicit split metrics (engagements24hTotal, replies24h, ...).
    This function normalizes both into one stable shape for downstream consumers.
    """
    src = raw_metrics if isinstance(raw_metrics, dict) else {}
    row = fallback_row or {}

    def _pick(*keys, default=None):
        for k in keys:
            if k in src and src[k] is not None:
                return src[k]
        return default

    engagements_24h_total = _pick("engagements24hTotal", "engagements24h", default=row.get("engagements_24h"))
    engagements_7d_total = _pick("engagements7dTotal", "engagements7d", default=row.get("engagements_7d"))
    posts_24h_total = _pick("posts24hTotal", "posts24h", default=row.get("posts_24h"))
    posts_7d_total = _pick("posts7dTotal", "posts7d", default=row.get("posts_7d"))
    reply_perf = _pick("replyPerformances24h", "replyPerformances", default=[])

    normalized = {
        "schemaVersion": 2,
        "date": _pick("date", default=row.get("eval_date").isoformat() if isinstance(row.get("eval_date"), date) else row.get("eval_date")),
        "timestamp": _pick("timestamp"),
        "operationalTargets": _pick("operationalTargets", default={}),
        "followerCount": _pick("followerCount", default=row.get("follower_count")),
        "followerGrowth": _pick("followerGrowth", default=row.get("follower_growth")),
        "prevEvalDate": _pick("prevEvalDate"),
        "engagements24hTotal": int(engagements_24h_total or 0),
        "replies24h": int(_pick("replies24h", default=engagements_24h_total) or 0),
        "likes24h": int(_pick("likes24h", default=0) or 0),
        "engagements7dTotal": int(engagements_7d_total or 0),
        "replies7d": int(_pick("replies7d", default=engagements_7d_total) or 0),
        "likes7d": int(_pick("likes7d", default=0) or 0),
        "posts24hTotal": int(posts_24h_total or 0),
        "originalPosts24h": int(_pick("originalPosts24h", default=posts_24h_total) or 0),
        "threadRoots24h": int(_pick("threadRoots24h", default=0) or 0),
        "threadReplies24h": int(_pick("threadReplies24h", default=0) or 0),
        "posts7dTotal": int(posts_7d_total or 0),
        "originalPosts7d": int(_pick("originalPosts7d", default=posts_7d_total) or 0),
        "threadRoots7d": int(_pick("threadRoots7d", default=0) or 0),
        "threadReplies7d": int(_pick("threadReplies7d", default=0) or 0),
        "postStatsRefreshed": int(_pick("postStatsRefreshed", default=0) or 0),
        "replyPerfRefresh": _pick("replyPerfRefresh", default={}),
        "replyPerformances24h": reply_perf if isinstance(reply_perf, list) else [],
    }
    return normalized


def insert_eval(
    conn,
    eval_date,
    follower_count: int | None,
    follower_growth: int | None,
    engagements_24h: int,
    engagements_7d: int,
    posts_24h: int,
    posts_7d: int,
    evaluation: str,
    raw_metrics: dict,
) -> None:
    """Insert a daily evaluation record. Ignores duplicate dates."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO eval_history (
                eval_date, follower_count, follower_growth,
                engagements_24h, engagements_7d,
                posts_24h, posts_7d,
                evaluation, raw_metrics, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (eval_date) DO UPDATE SET
                follower_count = EXCLUDED.follower_count,
                follower_growth = EXCLUDED.follower_growth,
                engagements_24h = EXCLUDED.engagements_24h,
                engagements_7d = EXCLUDED.engagements_7d,
                posts_24h = EXCLUDED.posts_24h,
                posts_7d = EXCLUDED.posts_7d,
                evaluation = EXCLUDED.evaluation,
                raw_metrics = EXCLUDED.raw_metrics
            """,
            (
                eval_date,
                follower_count,
                follower_growth,
                engagements_24h,
                engagements_7d,
                posts_24h,
                posts_7d,
                evaluation,
                json.dumps(raw_metrics),
            ),
        )


def get_recent_evals(conn, limit: int = 7) -> list[dict]:
    """Fetch the most recent evaluation records."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM eval_history
            ORDER BY eval_date DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    # Return in chronological order (oldest first)
    result = []
    for row in reversed(rows):
        d = dict(row)
        # Deserialize raw_metrics JSONB back to dict if needed
        if d.get("raw_metrics") and isinstance(d["raw_metrics"], str):
            try:
                d["raw_metrics"] = json.loads(d["raw_metrics"])
            except Exception:
                pass
        d["raw_metrics_normalized"] = normalize_eval_metrics(
            d.get("raw_metrics"),
            fallback_row=d,
        )
        # Serialize date/datetime fields so callers can json.dumps the result
        for k, v in d.items():
            if isinstance(v, (datetime, date)):
                d[k] = v.isoformat()
        result.append(d)
    return result


def get_last_eval(conn) -> dict | None:
    """Fetch the most recent evaluation record."""
    evals = get_recent_evals(conn, limit=1)
    return evals[0] if evals else None


def backfill_eval_metrics(conn, *, dry_run: bool = True, limit: int | None = None) -> dict[str, int]:
    """Rewrite eval_history.raw_metrics into canonical schema via normalize_eval_metrics().

    Returns counters:
      - scanned: rows examined
      - updated: rows changed (or that would change in dry-run)
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        sql = """
            SELECT id, eval_date, follower_count, follower_growth,
                   engagements_24h, engagements_7d, posts_24h, posts_7d, raw_metrics
            FROM eval_history
            ORDER BY eval_date DESC
        """
        params: tuple[Any, ...] = ()
        if limit is not None and limit > 0:
            sql += " LIMIT %s"
            params = (limit,)
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

    scanned = 0
    updated = 0
    for row in rows:
        scanned += 1
        raw = row.get("raw_metrics")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}

        normalized = normalize_eval_metrics(raw, fallback_row=row)
        if raw == normalized:
            continue
        updated += 1
        if dry_run:
            continue
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE eval_history SET raw_metrics = %s WHERE id = %s",
                (json.dumps(normalized), row["id"]),
            )

    return {"scanned": scanned, "updated": updated}


def get_daily_analytics_series(conn, days: int = 30) -> list[dict]:
    """Return per-day follower/reply/post analytics merged into one series.

    Output rows:
      {date, followers, follower_growth, engagements_total, replies, likes_only,
       posts_total, original_posts, thread_roots, thread_replies}
    """
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=max(1, days))).replace(hour=0, minute=0, second=0, microsecond=0)

    # Followers from eval_history (one row/day by schema).
    follower_map: dict[str, dict[str, int | None]] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT eval_date, follower_count, follower_growth, raw_metrics
            FROM eval_history
            WHERE eval_date >= %s
            ORDER BY eval_date ASC
            """,
            (since.date(),),
        )
        for row in cur.fetchall():
            d = dict(row)
            raw = d.get("raw_metrics")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = {}
            normalized = normalize_eval_metrics(raw if isinstance(raw, dict) else {}, fallback_row=d)
            day = (
                d["eval_date"].isoformat()
                if isinstance(d.get("eval_date"), date)
                else str(d.get("eval_date"))
            )
            follower_map[day] = {
                "followers": normalized.get("followerCount"),
                "follower_growth": normalized.get("followerGrowth"),
            }

    # Engagement counts by day.
    engagement_map: dict[str, dict[str, int]] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                (replied_at AT TIME ZONE 'UTC')::date AS day,
                COUNT(*)::int AS engagements_total,
                COUNT(*) FILTER (WHERE COALESCE(source, '') <> 'like')::int AS replies,
                COUNT(*) FILTER (WHERE source = 'like')::int AS likes_only
            FROM engagements
            WHERE replied_at >= %s
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            (since,),
        )
        for row in cur.fetchall():
            day = row["day"].isoformat()
            engagement_map[day] = {
                "engagements_total": int(row.get("engagements_total") or 0),
                "replies": int(row.get("replies") or 0),
                "likes_only": int(row.get("likes_only") or 0),
            }

    # Post counts by day.
    post_map: dict[str, dict[str, int]] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                (posted_at AT TIME ZONE 'UTC')::date AS day,
                COUNT(*)::int AS posts_total,
                COUNT(*) FILTER (WHERE type = 'post')::int AS original_posts,
                COUNT(*) FILTER (WHERE type = 'thread')::int AS thread_roots,
                COUNT(*) FILTER (WHERE type = 'thread_reply')::int AS thread_replies
            FROM posts
            WHERE posted_at >= %s
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            (since,),
        )
        for row in cur.fetchall():
            day = row["day"].isoformat()
            post_map[day] = {
                "posts_total": int(row.get("posts_total") or 0),
                "original_posts": int(row.get("original_posts") or 0),
                "thread_roots": int(row.get("thread_roots") or 0),
                "thread_replies": int(row.get("thread_replies") or 0),
            }

    # Merge over full day range.
    start_day = since.date()
    end_day = now.date()
    series: list[dict] = []
    day = start_day
    while day <= end_day:
        key = day.isoformat()
        series.append(
            {
                "date": key,
                "followers": follower_map.get(key, {}).get("followers"),
                "follower_growth": follower_map.get(key, {}).get("follower_growth"),
                "engagements_total": engagement_map.get(key, {}).get("engagements_total", 0),
                "replies": engagement_map.get(key, {}).get("replies", 0),
                "likes_only": engagement_map.get(key, {}).get("likes_only", 0),
                "posts_total": post_map.get(key, {}).get("posts_total", 0),
                "original_posts": post_map.get(key, {}).get("original_posts", 0),
                "thread_roots": post_map.get(key, {}).get("thread_roots", 0),
                "thread_replies": post_map.get(key, {}).get("thread_replies", 0),
            }
        )
        day += timedelta(days=1)
    return series


# ---------------------------------------------------------------------------
# Schema init (inline DDL — does not read schema.sql)
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    username            TEXT PRIMARY KEY,
    display_name        TEXT,
    bio                 TEXT,
    follower_count      INTEGER,
    following_count     INTEGER,
    relevance_score     REAL,
    relevance_notes     TEXT,
    discovery_source    TEXT,
    discovered_via      TEXT,
    discovered_at       TIMESTAMPTZ  DEFAULT now(),
    stage               TEXT         NOT NULL DEFAULT 'candidate',
    followed_at         TIMESTAMPTZ,
    follows_us_back     BOOLEAN      DEFAULT FALSE,
    follows_checked_at  TIMESTAMPTZ,
    engagement_count    INTEGER      DEFAULT 0,
    reply_back_count    INTEGER      DEFAULT 0,
    last_engaged_at     TIMESTAMPTZ,
    last_seen_tweet_at  TIMESTAMPTZ,
    skip_reason         TEXT,
    extra               JSONB
);

CREATE TABLE IF NOT EXISTS social_edges (
    source   TEXT        NOT NULL,
    target   TEXT        NOT NULL,
    type     TEXT        NOT NULL,
    seen_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (source, target, type)
);

CREATE TABLE IF NOT EXISTS engagements (
    tweet_id              TEXT PRIMARY KEY,
    target_username       TEXT,
    target_tweet_text     TEXT,
    tweet_url             TEXT,
    tweet_likes           INTEGER DEFAULT 0,
    tweet_rts             INTEGER DEFAULT 0,
    tweet_replies         INTEGER DEFAULT 0,
    our_reply_text        TEXT,
    our_reply_id          TEXT,
    replied_at            TIMESTAMPTZ DEFAULT now(),
    source                TEXT,
    search_term           TEXT,
    conv_likelihood       INTEGER,
    profile_click_worthy  BOOLEAN,
    llm_reasoning         TEXT,
    reply_likes           INTEGER DEFAULT 0,
    reply_rts             INTEGER DEFAULT 0,
    reply_replies         INTEGER DEFAULT 0,
    got_reply_back        BOOLEAN DEFAULT FALSE,
    perf_checked_at       TIMESTAMPTZ
);

ALTER TABLE engagements ADD COLUMN IF NOT EXISTS target_tweet_text TEXT;
ALTER TABLE engagements ADD COLUMN IF NOT EXISTS tweet_url         TEXT;
ALTER TABLE engagements ADD COLUMN IF NOT EXISTS tweet_likes       INTEGER DEFAULT 0;
ALTER TABLE engagements ADD COLUMN IF NOT EXISTS tweet_rts         INTEGER DEFAULT 0;
ALTER TABLE engagements ADD COLUMN IF NOT EXISTS tweet_replies     INTEGER DEFAULT 0;

CREATE TABLE IF NOT EXISTS posts (
    tweet_id        TEXT PRIMARY KEY,
    posted_at       TIMESTAMPTZ DEFAULT now(),
    type            TEXT,
    text            TEXT,
    url             TEXT,
    likes           INTEGER DEFAULT 0,
    rts             INTEGER DEFAULT 0,
    perf_checked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS search_term_stats (
    term              TEXT PRIMARY KEY,
    candidates_seen   INTEGER DEFAULT 0,
    engaged           INTEGER DEFAULT 0,
    perf_measured     INTEGER DEFAULT 0,
    total_likes       INTEGER DEFAULT 0,
    total_reply_backs INTEGER DEFAULT 0,
    zero_perf_replies INTEGER DEFAULT 0,
    last_used_at      TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_history (
    id              SERIAL PRIMARY KEY,
    eval_date       DATE        UNIQUE NOT NULL,
    follower_count  INTEGER,
    follower_growth INTEGER,
    engagements_24h INTEGER,
    engagements_7d  INTEGER,
    posts_24h       INTEGER,
    posts_7d        INTEGER,
    evaluation      TEXT,
    raw_metrics     JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS candidate_queue (
    tweet_id        TEXT PRIMARY KEY,
    author          TEXT,
    text            TEXT,
    search_term     TEXT,
    url             TEXT,
    tweet_datetime  TIMESTAMPTZ,
    likes           INTEGER DEFAULT 0,
    retweets        INTEGER DEFAULT 0,
    replies         INTEGER DEFAULT 0,
    queued_at       TIMESTAMPTZ DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS engagement_pipeline_queue (
    tweet_id        TEXT PRIMARY KEY,
    author          TEXT,
    text            TEXT,
    search_term     TEXT,
    url             TEXT,
    likes           INTEGER DEFAULT 0,
    retweets        INTEGER DEFAULT 0,
    replies         INTEGER DEFAULT 0,
    candidate_json  TEXT,
    context_json    TEXT,
    decision_json   TEXT,
    status          TEXT        NOT NULL DEFAULT 'prepared',
    prepared_at     TIMESTAMPTZ,
    analyzed_at     TIMESTAMPTZ,
    posted_at       TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT now(),
    error           TEXT
);

CREATE TABLE IF NOT EXISTS kv_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_queue_unprocessed ON candidate_queue(queued_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_eng_pipe_status ON engagement_pipeline_queue(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_accounts_stage  ON accounts(stage);
CREATE INDEX IF NOT EXISTS idx_accounts_score  ON accounts(relevance_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_accounts_source ON accounts(discovery_source);
CREATE INDEX IF NOT EXISTS idx_edges_source    ON social_edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target    ON social_edges(target);
CREATE INDEX IF NOT EXISTS idx_eng_user        ON engagements(target_username);
CREATE INDEX IF NOT EXISTS idx_eng_time        ON engagements(replied_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_time      ON posts(posted_at DESC);

CREATE TABLE IF NOT EXISTS tweet_replies (
    reply_tweet_id  TEXT PRIMARY KEY,
    parent_tweet_id TEXT NOT NULL,
    author          TEXT,
    text            TEXT,
    likes           INTEGER DEFAULT 0,
    retweets        INTEGER DEFAULT 0,
    replies         INTEGER DEFAULT 0,
    scraped_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tweet_replies_parent ON tweet_replies(parent_tweet_id);
CREATE INDEX IF NOT EXISTS idx_tweet_replies_eng    ON tweet_replies((likes + retweets) DESC);

CREATE TABLE IF NOT EXISTS tweet_context_cache (
    tweet_id  TEXT PRIMARY KEY,
    context   TEXT NOT NULL,   -- JSON blob
    cached_at TEXT NOT NULL    -- ISO-8601 UTC timestamp
);
"""


# ---------------------------------------------------------------------------
# Candidate queue (filled by search_queue.py via twscrape)
# ---------------------------------------------------------------------------


def insert_candidate_queue(conn, candidates: list[dict]) -> int:
    """Bulk-insert candidates into candidate_queue. Returns count of new rows."""
    inserted = 0
    for c in candidates:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO candidate_queue
                    (tweet_id, author, text, search_term, url,
                     tweet_datetime, likes, retweets, replies)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tweet_id) DO NOTHING
                """,
                (
                    c["tweet_id"],
                    c.get("author"),
                    c.get("text"),
                    c.get("search_term"),
                    c.get("url"),
                    c.get("tweet_datetime"),
                    c.get("likes", 0),
                    c.get("retweets", 0),
                    c.get("replies", 0),
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
    return inserted


def get_queued_candidates(conn, limit: int = 100) -> list[dict]:
    """Fetch unprocessed candidates whose tweet is < 6 hours old and not yet engaged.

    Uses the Snowflake ID to compute tweet creation time so stale queue
    entries (queued but never consumed) are automatically excluded.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT tweet_id, author, text, search_term, url,
                   likes, retweets, replies
            FROM candidate_queue
            WHERE processed_at IS NULL
              AND tweet_id NOT IN (SELECT tweet_id FROM engagements)
              AND to_timestamp(((tweet_id::bigint >> 22) + 1288834974657) / 1000.0)
                    > now() - interval '24 hours'
            ORDER BY queued_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "tweetId": row["tweet_id"],
            "author": row["author"] or "unknown",
            "text": row["text"] or "",
            "searchTerm": row["search_term"] or "",
            "url": row["url"] or f"https://x.com/i/web/status/{row['tweet_id']}",
            "likes": row["likes"] or 0,
            "retweets": row["retweets"] or 0,
            "replies": row["replies"] or 0,
        }
        for row in rows
    ]


def mark_queue_processed(conn, tweet_ids: list[str]) -> None:
    """Mark candidate_queue entries as processed."""
    if not tweet_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE candidate_queue SET processed_at = now() WHERE tweet_id = ANY(%s)",
            (tweet_ids,),
        )


def queue_size(conn) -> int:
    """Count unprocessed candidates whose tweet is < 24 hours old."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM candidate_queue cq
            WHERE cq.processed_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM engagements e WHERE e.tweet_id = cq.tweet_id)
              AND to_timestamp(((cq.tweet_id::bigint >> 22) + 1288834974657) / 1000.0)
                    > now() - interval '24 hours'
            """
        )
        row = cur.fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# Engagement pipeline queue (prepare -> analyze -> post)
# ---------------------------------------------------------------------------


def upsert_prepared_candidate(conn, candidate: dict, tweet_context: dict) -> None:
    """Insert or update a prepared candidate ready for LLM analysis."""
    tweet_id = str(candidate.get("tweetId") or "")
    if not tweet_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO engagement_pipeline_queue (
                tweet_id, author, text, search_term, url, likes, retweets, replies,
                candidate_json, context_json, status, prepared_at, updated_at, error
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'prepared', now(), now(), NULL)
            ON CONFLICT (tweet_id) DO UPDATE SET
                author = EXCLUDED.author,
                text = EXCLUDED.text,
                search_term = EXCLUDED.search_term,
                url = EXCLUDED.url,
                likes = EXCLUDED.likes,
                retweets = EXCLUDED.retweets,
                replies = EXCLUDED.replies,
                candidate_json = EXCLUDED.candidate_json,
                context_json = EXCLUDED.context_json,
                status = 'prepared',
                prepared_at = now(),
                analyzed_at = NULL,
                decision_json = NULL,
                posted_at = NULL,
                updated_at = now(),
                error = NULL
            """,
            (
                tweet_id,
                candidate.get("author") or "unknown",
                (candidate.get("text") or "")[:500],
                candidate.get("searchTerm") or "",
                candidate.get("url") or f"https://x.com/i/web/status/{tweet_id}",
                candidate.get("likes", 0),
                candidate.get("retweets", 0),
                candidate.get("replies", 0),
                json.dumps(candidate, ensure_ascii=False, default=str),
                json.dumps(tweet_context, ensure_ascii=False, default=str),
            ),
        )


def get_pipeline_items_by_status(conn, status: str, limit: int = 50) -> list[dict]:
    """Fetch pipeline records for a given status."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT tweet_id, author, text, search_term, url, likes, retweets, replies,
                   candidate_json, context_json, decision_json, status,
                   prepared_at, analyzed_at, posted_at, updated_at, error
            FROM engagement_pipeline_queue
            WHERE status = %s
              AND to_timestamp(((tweet_id::bigint >> 22) + 1288834974657) / 1000.0)
                    > now() - interval '24 hours'
            ORDER BY updated_at ASC
            LIMIT %s
            """,
            (status, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def update_pipeline_analysis(
    conn,
    tweet_id: str,
    decision: dict | None,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Persist analysis result for a prepared candidate."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE engagement_pipeline_queue
            SET decision_json = %s,
                status = %s,
                analyzed_at = now(),
                updated_at = now(),
                error = %s
            WHERE tweet_id = %s
            """,
            (
                json.dumps(decision, ensure_ascii=False, default=str)
                if decision is not None
                else None,
                status,
                error,
                tweet_id,
            ),
        )


def mark_pipeline_posted(conn, tweet_id: str) -> None:
    """Mark pipeline item as posted."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE engagement_pipeline_queue
            SET status = 'posted',
                posted_at = now(),
                updated_at = now(),
                error = NULL
            WHERE tweet_id = %s
            """,
            (tweet_id,),
        )


def mark_pipeline_post_failed(conn, tweet_id: str, error: str) -> None:
    """Mark pipeline item as post_failed with a reason."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE engagement_pipeline_queue
            SET status = 'post_failed',
                updated_at = now(),
                error = %s
            WHERE tweet_id = %s
            """,
            (error[:500], tweet_id),
        )


def mark_pipeline_skipped(conn, tweet_id: str, reason: str) -> None:
    """Mark pipeline item as skipped with reason."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE engagement_pipeline_queue
            SET status = 'skipped',
                updated_at = now(),
                error = %s
            WHERE tweet_id = %s
            """,
            (reason[:500], tweet_id),
        )


# ---------------------------------------------------------------------------
# Tweet context cache
# ---------------------------------------------------------------------------


def get_cached_tweet_context(conn, tweet_id: str, ttl_hours: int = 1) -> dict | None:
    """Return cached tweet context if fresh, else None."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT context, cached_at FROM tweet_context_cache WHERE tweet_id = %s",
            (tweet_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        cached_at = datetime.fromisoformat(row["cached_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - cached_at > timedelta(hours=ttl_hours):
            return None
    except (KeyError, ValueError):
        return None
    try:
        return json.loads(row["context"])
    except (json.JSONDecodeError, TypeError):
        return None


def store_tweet_context(conn, tweet_id: str, context: dict) -> None:
    """Upsert tweet context into cache."""
    cached_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tweet_context_cache (tweet_id, context, cached_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (tweet_id) DO UPDATE SET
                context   = EXCLUDED.context,
                cached_at = EXCLUDED.cached_at
            """,
            (tweet_id, json.dumps(context, ensure_ascii=False), cached_at),
        )


def ensure_schema(conn) -> None:
    """Run CREATE TABLE IF NOT EXISTS DDL and seed initial accounts.

    Safe to call on every startup — all statements are idempotent.
    """
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)

    # Seed accounts: insert candidates that don't exist yet
    for acct in SEED_ACCOUNTS:
        # Only insert if not already present — never overwrite existing data
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM accounts WHERE username = %s LIMIT 1",
                (acct["username"],),
            )
            exists = cur.fetchone() is not None

        if not exists:
            upsert_account(
                conn,
                acct["username"],
                discovery_source=acct["discovery_source"],
                relevance_notes=acct["relevance_notes"],
                stage="candidate",
            )

    # Release DDL locks immediately. Without this explicit commit, long-running
    # flows can hold relation locks for minutes and block read-only CLI tools.
    conn.commit()

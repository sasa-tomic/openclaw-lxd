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
# Typed runtime state/cache (no JSON state blobs)
# ---------------------------------------------------------------------------


def get_thread_index_map(conn) -> dict[str, str]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT tweet_id, note_path FROM thread_index")
        rows = cur.fetchall()
    return {str(r["tweet_id"]): str(r["note_path"]) for r in rows}


def upsert_thread_index(conn, tweet_id: str, note_path: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO thread_index (tweet_id, note_path, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (tweet_id) DO UPDATE SET
                note_path = EXCLUDED.note_path,
                updated_at = now()
            """,
            (tweet_id, note_path),
        )


def get_thread_note_path(conn, tweet_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT note_path FROM thread_index WHERE tweet_id = %s", (tweet_id,))
        row = cur.fetchone()
    return row[0] if row else None


def get_profile_cache(conn, username: str) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT username, cached_at, display_name, bio, location, website,
                   followers_count, following_count, tweets_count,
                   is_verified, is_blue_verified, recent_tweets
            FROM profile_cache
            WHERE username = %s
            """,
            (username.lower(),),
        )
        row = cur.fetchone()
    if not row:
        return None
    return dict(row)


def upsert_profile_cache(conn, username: str, profile: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO profile_cache (
                username, cached_at, display_name, bio, location, website,
                followers_count, following_count, tweets_count,
                is_verified, is_blue_verified, recent_tweets
            ) VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (username) DO UPDATE SET
                cached_at = now(),
                display_name = EXCLUDED.display_name,
                bio = EXCLUDED.bio,
                location = EXCLUDED.location,
                website = EXCLUDED.website,
                followers_count = EXCLUDED.followers_count,
                following_count = EXCLUDED.following_count,
                tweets_count = EXCLUDED.tweets_count,
                is_verified = EXCLUDED.is_verified,
                is_blue_verified = EXCLUDED.is_blue_verified,
                recent_tweets = EXCLUDED.recent_tweets
            """,
            (
                username.lower(),
                profile.get("displayName"),
                profile.get("bio"),
                profile.get("location"),
                profile.get("website"),
                profile.get("followersCount"),
                profile.get("followingCount"),
                profile.get("tweetsCount"),
                bool(profile.get("isVerified", False)),
                bool(profile.get("isBlueVerified", False)),
                list(profile.get("recentTweets") or []),
            ),
        )


def prune_profile_cache(conn, *, older_than_days: int = 180) -> int:
    """Delete profile cache rows older than the configured retention window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM profile_cache
            WHERE cached_at < (now() - (%s::int * INTERVAL '1 day'))
            """,
            (max(1, int(older_than_days)),),
        )
        return int(cur.rowcount or 0)


def get_cached_search_results(conn, term: str, ttl_hours: int) -> list[dict] | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT cached_at
            FROM search_cache_terms
            WHERE term = %s
            """,
            (term.lower().strip(),),
        )
        head = cur.fetchone()
        if not head:
            return None
        cached_at = head.get("cached_at")
        if not isinstance(cached_at, datetime):
            return None
        if datetime.now(timezone.utc) - cached_at > timedelta(hours=ttl_hours):
            return None
        cur.execute(
            """
            SELECT tweet_id, author, text, url, tweet_datetime, likes, retweets, replies, ord
            FROM search_cache_results
            WHERE term = %s
            ORDER BY ord ASC
            """,
            (term.lower().strip(),),
        )
        rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        out.append(
            {
                "tweetId": d.get("tweet_id"),
                "author": d.get("author") or "unknown",
                "text": d.get("text") or "",
                "searchTerm": term,
                "url": d.get("url"),
                "datetime": (
                    d["tweet_datetime"].isoformat()
                    if isinstance(d.get("tweet_datetime"), datetime)
                    else d.get("tweet_datetime")
                ),
                "likes": int(d.get("likes") or 0),
                "retweets": int(d.get("retweets") or 0),
                "replies": int(d.get("replies") or 0),
            }
        )
    return out


def store_search_cache_results(conn, term: str, results: list[dict]) -> None:
    norm_term = term.lower().strip()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO search_cache_terms (term, cached_at)
            VALUES (%s, now())
            ON CONFLICT (term) DO UPDATE SET cached_at = now()
            """,
            (norm_term,),
        )
        cur.execute("DELETE FROM search_cache_results WHERE term = %s", (norm_term,))
        for i, r in enumerate(results):
            raw_dt = r.get("datetime")
            dt_val = None
            if raw_dt:
                try:
                    dt_val = datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
                except Exception:
                    dt_val = None
            cur.execute(
                """
                INSERT INTO search_cache_results (
                    term, tweet_id, author, text, url, tweet_datetime,
                    likes, retweets, replies, ord
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    norm_term,
                    r.get("tweetId"),
                    r.get("author"),
                    (r.get("text") or "")[:500],
                    r.get("url"),
                    dt_val,
                    int(r.get("likes") or 0),
                    int(r.get("retweets") or 0),
                    int(r.get("replies") or 0),
                    i,
                ),
            )


def get_content_queue(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, text, drafted_at, llm_score, posted, posted_at
            FROM content_queue
            ORDER BY drafted_at ASC, id ASC
            """
        )
        rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        out.append(
            {
                "id": d.get("id"),
                "text": d.get("text") or "",
                "draftedAt": (
                    d["drafted_at"].isoformat()
                    if isinstance(d.get("drafted_at"), datetime)
                    else None
                ),
                "llm_score": int(d.get("llm_score") or 0),
                "posted": bool(d.get("posted")),
                "postedAt": (
                    d["posted_at"].isoformat()
                    if isinstance(d.get("posted_at"), datetime)
                    else None
                ),
            }
        )
    return out


def insert_content_queue_entries(conn, entries: list[dict]) -> int:
    inserted = 0
    for e in entries:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_queue (text, drafted_at, llm_score, posted, posted_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    (e.get("text") or "")[:500],
                    datetime.fromisoformat(str(e.get("draftedAt")).replace("Z", "+00:00"))
                    if e.get("draftedAt")
                    else datetime.now(timezone.utc),
                    int(e.get("llm_score") or 0),
                    bool(e.get("posted", False)),
                    datetime.fromisoformat(str(e.get("postedAt")).replace("Z", "+00:00"))
                    if e.get("postedAt")
                    else None,
                ),
            )
            inserted += 1
    return inserted


def mark_content_queue_posted(conn, row_id: int, posted_at: datetime | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE content_queue
            SET posted = TRUE,
                posted_at = %s
            WHERE id = %s
            """,
            (posted_at or datetime.now(timezone.utc), row_id),
        )


def prune_content_queue(conn, posted_older_than_days: int = 7) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=posted_older_than_days)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM content_queue WHERE posted = TRUE AND posted_at IS NOT NULL AND posted_at < %s",
            (cutoff,),
        )
        return int(cur.rowcount or 0)


def get_morning_state(conn) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT last_research_run FROM twitter_morning_state WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return {"lastResearchRun": None}
    dt = row.get("last_research_run")
    return {
        "lastResearchRun": dt.isoformat() if isinstance(dt, datetime) else None,
    }


def set_morning_state_last_run(conn, run_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO twitter_morning_state (id, last_research_run)
            VALUES (1, %s)
            ON CONFLICT (id) DO UPDATE SET last_research_run = EXCLUDED.last_research_run
            """,
            (run_at,),
        )


def store_morning_research(conn, *, run_at: datetime, hn_stories: list[dict], dev_activity: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM twitter_morning_hn WHERE run_at = %s", (run_at,))
        for i, s in enumerate(hn_stories):
            cur.execute(
                """
                INSERT INTO twitter_morning_hn (run_at, ord, title, url, points)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (run_at, i, s.get("title"), s.get("url"), int(s.get("points") or 0)),
            )
        cur.execute(
            """
            INSERT INTO twitter_morning_latest (id, run_at, dev_activity)
            VALUES (1, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                run_at = EXCLUDED.run_at,
                dev_activity = EXCLUDED.dev_activity
            """,
            (run_at, dev_activity),
        )


def get_latest_morning_research(conn) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT run_at, dev_activity FROM twitter_morning_latest WHERE id = 1")
        head = cur.fetchone()
        if not head:
            return None
        run_at = head.get("run_at")
        cur.execute(
            """
            SELECT title, url, points
            FROM twitter_morning_hn
            WHERE run_at = %s
            ORDER BY ord ASC
            """,
            (run_at,),
        )
        stories = [dict(r) for r in cur.fetchall()]
    return {
        "timestamp": run_at.isoformat() if isinstance(run_at, datetime) else None,
        "hnStories": stories,
        "devActivity": head.get("dev_activity"),
    }


def insert_recent_post_log(
    conn,
    *,
    activity_type: str,
    text: str,
    link: str | None = None,
    tweet_id: str | None = None,
    created_at: datetime | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recent_post_log (created_at, activity_type, text, link, tweet_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (created_at or datetime.now(timezone.utc), activity_type, text[:500], link, tweet_id),
        )


def get_recent_post_log(conn, limit: int = 200) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT created_at, activity_type, text, link, tweet_id
            FROM recent_post_log
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        out.append(
            {
                "date": d["created_at"].date().isoformat()
                if isinstance(d.get("created_at"), datetime)
                else "",
                "type": d.get("activity_type", ""),
                "text": d.get("text", ""),
                "link": d.get("link"),
                "tweetId": d.get("tweet_id"),
            }
        )
    return out


def prune_recent_post_log(conn, *, older_than_days: int = 90) -> int:
    """Delete recent_post_log rows older than the configured retention window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM recent_post_log
            WHERE created_at < (now() - (%s::int * INTERVAL '1 day'))
            """,
            (max(1, int(older_than_days)),),
        )
        return int(cur.rowcount or 0)


def get_reply_monitor_seen(conn) -> dict[str, str]:
    """Return reply monitor seen tweet IDs as {tweet_id: iso_timestamp}."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT tweet_id, seen_at
            FROM reply_monitor_seen
            ORDER BY seen_at DESC
            """
        )
        rows = cur.fetchall()
    out: dict[str, str] = {}
    for row in rows:
        seen_at = row.get("seen_at")
        if isinstance(seen_at, datetime):
            out[str(row["tweet_id"])] = seen_at.isoformat()
    return out


def add_reply_monitor_seen(conn, tweet_id: str, *, seen_at: datetime | None = None) -> None:
    """Mark a mention tweet as seen (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reply_monitor_seen (tweet_id, seen_at)
            VALUES (%s, %s)
            ON CONFLICT (tweet_id) DO UPDATE SET
                seen_at = EXCLUDED.seen_at
            """,
            (str(tweet_id), seen_at or datetime.now(timezone.utc)),
        )


def prune_reply_monitor_seen(conn, *, older_than_days: int = 2) -> int:
    """Delete seen mention rows older than retention window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM reply_monitor_seen
            WHERE seen_at < (now() - (%s::int * INTERVAL '1 day'))
            """,
            (max(1, int(older_than_days)),),
        )
        return int(cur.rowcount or 0)


def get_target_monitor_account_state(conn, username: str) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT last_checked_at, last_tweet_id, last_tweet_at
            FROM target_monitor_accounts
            WHERE username = %s
            """,
            (username.lower(),),
        )
        row = cur.fetchone()
    if not row:
        return {"lastCheckedAt": None, "lastTweetId": None, "lastTweetAt": None}
    return {
        "lastCheckedAt": (
            row["last_checked_at"].isoformat()
            if isinstance(row.get("last_checked_at"), datetime)
            else None
        ),
        "lastTweetId": row.get("last_tweet_id"),
        "lastTweetAt": (
            row["last_tweet_at"].isoformat()
            if isinstance(row.get("last_tweet_at"), datetime)
            else None
        ),
    }


def set_target_monitor_account_state(conn, username: str, data: dict) -> None:
    def _to_dt(v):
        if not v:
            return None
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            return None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO target_monitor_accounts (username, last_checked_at, last_tweet_id, last_tweet_at, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (username) DO UPDATE SET
                last_checked_at = EXCLUDED.last_checked_at,
                last_tweet_id = EXCLUDED.last_tweet_id,
                last_tweet_at = EXCLUDED.last_tweet_at,
                updated_at = now()
            """,
            (
                username.lower(),
                _to_dt(data.get("lastCheckedAt")),
                data.get("lastTweetId"),
                _to_dt(data.get("lastTweetAt")),
            ),
        )


def get_target_monitor_replied_ids(conn, limit: int = 500) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tweet_id
            FROM target_monitor_replied
            ORDER BY replied_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return {str(r[0]) for r in rows}


def add_target_monitor_replied_id(conn, tweet_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO target_monitor_replied (tweet_id, replied_at)
            VALUES (%s, now())
            ON CONFLICT (tweet_id) DO NOTHING
            """,
            (tweet_id,),
        )


def trim_target_monitor_replied(conn, keep: int = 500) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM target_monitor_replied
            WHERE tweet_id IN (
                SELECT tweet_id
                FROM target_monitor_replied
                ORDER BY replied_at DESC
                OFFSET %s
            )
            """,
            (keep,),
        )


def set_target_monitor_last_run(conn, when: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO target_monitor_meta (id, last_run_at, updated_at)
            VALUES (1, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                last_run_at = EXCLUDED.last_run_at,
                updated_at = now()
            """,
            (when,),
        )


def get_cdp_health_state(conn) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT down, since, last_check FROM twitter_cdp_health_state WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return {"down": False, "since": None, "last_check": None}
    return {
        "down": bool(row.get("down")),
        "since": row["since"].strftime("%Y-%m-%dT%H:%M:%S%z") if row.get("since") else None,
        "last_check": row["last_check"].strftime("%Y-%m-%dT%H:%M:%S%z") if row.get("last_check") else None,
    }


def set_cdp_health_state(conn, *, down: bool, since: str | None, last_check: str | None) -> None:
    def _parse(v: str | None):
        if not v:
            return None
        try:
            return datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z")
        except Exception:
            return None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO twitter_cdp_health_state (id, down, since, last_check, updated_at)
            VALUES (1, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                down = EXCLUDED.down,
                since = EXCLUDED.since,
                last_check = EXCLUDED.last_check,
                updated_at = now()
            """,
            (down, _parse(since), _parse(last_check)),
        )


def get_repair_last_map(conn) -> dict[str, str]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT flow_name, last_repair_at FROM twitter_repair_last")
        rows = cur.fetchall()
    out: dict[str, str] = {}
    for r in rows:
        dt = r.get("last_repair_at")
        out[str(r.get("flow_name"))] = dt.isoformat() if isinstance(dt, datetime) else str(dt)
    return out


def upsert_repair_last(conn, flow_name: str, when: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO twitter_repair_last (flow_name, last_repair_at)
            VALUES (%s, %s)
            ON CONFLICT (flow_name) DO UPDATE SET last_repair_at = EXCLUDED.last_repair_at
            """,
            (flow_name, when),
        )


def append_repair_history(conn, entry: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO twitter_repair_history (
                happened_at, flow_name, flow_run_id, error_summary,
                opencode_exit_code, test_result, merged, worktree_branch, duration_seconds
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00"))
                if entry.get("timestamp")
                else datetime.now(timezone.utc),
                entry.get("flowName"),
                entry.get("flowRunId"),
                entry.get("errorSummary"),
                entry.get("opencodeExitCode"),
                entry.get("testResult"),
                bool(entry.get("merged")),
                entry.get("worktreeBranch"),
                int(entry.get("durationSeconds") or 0),
            ),
        )


def trim_repair_history(conn, keep: int = 100) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM twitter_repair_history
            WHERE id IN (
                SELECT id
                FROM twitter_repair_history
                ORDER BY happened_at DESC
                OFFSET %s
            )
            """,
            (keep,),
        )


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


def get_accounts_needing_followback_check(
    conn,
    *,
    min_hours_between_checks: int = 72,
    limit: int = 3,
    exclude_username: str | None = None,
) -> list[str]:
    """Return followed/engaged accounts whose follow-back status is stale."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT username
            FROM accounts
            WHERE stage IN ('followed', 'engaged', 'warm', 'follower')
              AND (%s IS NULL OR LOWER(username) <> LOWER(%s))
              AND (
                follows_checked_at IS NULL
                OR follows_checked_at < (now() - (%s::int * INTERVAL '1 hour'))
              )
            ORDER BY follows_checked_at ASC NULLS FIRST, followed_at ASC NULLS FIRST
            LIMIT %s
            """,
            (
                exclude_username,
                exclude_username,
                max(1, int(min_hours_between_checks)),
                max(1, int(limit)),
            ),
        )
        return [str(row[0]) for row in cur.fetchall()]


def set_account_last_seen_tweet_at(conn, username: str, seen_at: datetime) -> None:
    """Upsert last_seen_tweet_at, keeping the most recent timestamp."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts (username, last_seen_tweet_at)
            VALUES (%s, %s)
            ON CONFLICT (username) DO UPDATE
              SET last_seen_tweet_at = GREATEST(
                    COALESCE(accounts.last_seen_tweet_at, '-infinity'::timestamptz),
                    EXCLUDED.last_seen_tweet_at
              )
            """,
            (username, seen_at),
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

CREATE TABLE IF NOT EXISTS thread_index (
    tweet_id   TEXT PRIMARY KEY,
    note_path  TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profile_cache (
    username         TEXT PRIMARY KEY,
    cached_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    display_name     TEXT,
    bio              TEXT,
    location         TEXT,
    website          TEXT,
    followers_count  INTEGER,
    following_count  INTEGER,
    tweets_count     INTEGER,
    is_verified      BOOLEAN DEFAULT FALSE,
    is_blue_verified BOOLEAN DEFAULT FALSE,
    recent_tweets    TEXT[] DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS search_cache_terms (
    term      TEXT PRIMARY KEY,
    cached_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS search_cache_results (
    term           TEXT NOT NULL REFERENCES search_cache_terms(term) ON DELETE CASCADE,
    tweet_id       TEXT NOT NULL,
    author         TEXT,
    text           TEXT,
    url            TEXT,
    tweet_datetime TIMESTAMPTZ,
    likes          INTEGER DEFAULT 0,
    retweets       INTEGER DEFAULT 0,
    replies        INTEGER DEFAULT 0,
    ord            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (term, tweet_id)
);

CREATE TABLE IF NOT EXISTS content_queue (
    id         BIGSERIAL PRIMARY KEY,
    text       TEXT NOT NULL,
    drafted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    llm_score  INTEGER DEFAULT 0,
    posted     BOOLEAN NOT NULL DEFAULT FALSE,
    posted_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS twitter_morning_state (
    id                SMALLINT PRIMARY KEY,
    last_research_run TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS twitter_morning_latest (
    id           SMALLINT PRIMARY KEY,
    run_at       TIMESTAMPTZ NOT NULL,
    dev_activity TEXT
);

CREATE TABLE IF NOT EXISTS twitter_morning_hn (
    run_at TIMESTAMPTZ NOT NULL,
    ord    INTEGER NOT NULL,
    title  TEXT NOT NULL,
    url    TEXT,
    points INTEGER DEFAULT 0,
    PRIMARY KEY (run_at, ord)
);

CREATE TABLE IF NOT EXISTS recent_post_log (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    activity_type TEXT NOT NULL,
    text          TEXT NOT NULL,
    link          TEXT,
    tweet_id      TEXT
);

CREATE TABLE IF NOT EXISTS reply_monitor_seen (
    tweet_id TEXT PRIMARY KEY,
    seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS target_monitor_accounts (
    username        TEXT PRIMARY KEY,
    last_checked_at TIMESTAMPTZ,
    last_tweet_id   TEXT,
    last_tweet_at   TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS target_monitor_replied (
    tweet_id   TEXT PRIMARY KEY,
    replied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS target_monitor_meta (
    id          SMALLINT PRIMARY KEY,
    last_run_at TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS twitter_cdp_health_state (
    id         SMALLINT PRIMARY KEY,
    down       BOOLEAN NOT NULL DEFAULT FALSE,
    since      TIMESTAMPTZ,
    last_check TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS twitter_repair_last (
    flow_name      TEXT PRIMARY KEY,
    last_repair_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS twitter_repair_history (
    id                 BIGSERIAL PRIMARY KEY,
    happened_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    flow_name          TEXT NOT NULL,
    flow_run_id        TEXT NOT NULL,
    error_summary      TEXT,
    opencode_exit_code INTEGER,
    test_result        TEXT,
    merged             BOOLEAN DEFAULT FALSE,
    worktree_branch    TEXT,
    duration_seconds   INTEGER DEFAULT 0
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
CREATE INDEX IF NOT EXISTS idx_search_cache_results_term_ord ON search_cache_results(term, ord);
CREATE INDEX IF NOT EXISTS idx_content_queue_posted ON content_queue(posted, drafted_at);
CREATE INDEX IF NOT EXISTS idx_recent_post_log_time ON recent_post_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reply_monitor_seen_time ON reply_monitor_seen(seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_target_monitor_replied_time ON target_monitor_replied(replied_at DESC);
CREATE INDEX IF NOT EXISTS idx_repair_history_time ON twitter_repair_history(happened_at DESC);

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

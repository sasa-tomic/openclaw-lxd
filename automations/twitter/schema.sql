-- Social graph / account registry
CREATE TABLE IF NOT EXISTS accounts (
    username            TEXT PRIMARY KEY,
    display_name        TEXT,
    bio                 TEXT,
    follower_count      INTEGER,
    following_count     INTEGER,
    relevance_score     REAL,
    relevance_notes     TEXT,
    discovery_source    TEXT,        -- 'seed', 'social_graph', 'search', 'list', 'engagement'
    discovered_via      TEXT,        -- which account led to discovery
    discovered_at       TIMESTAMPTZ  DEFAULT now(),
    stage               TEXT         NOT NULL DEFAULT 'candidate',
    -- stages: candidate -> scored -> followed -> engaged -> warm -> follower -> blocked
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

-- Social graph edges (who interacts with whom)
CREATE TABLE IF NOT EXISTS social_edges (
    source   TEXT        NOT NULL,
    target   TEXT        NOT NULL,
    type     TEXT        NOT NULL,   -- 'replies_to', 'follows', 'rt'
    seen_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (source, target, type)
);

-- Our engagement history (replies we posted)
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
    source                TEXT,   -- 'timeline', 'search', 'target_monitor', 'mention', 'direct_reply'
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

-- Original posts we published
CREATE TABLE IF NOT EXISTS posts (
    tweet_id        TEXT PRIMARY KEY,
    posted_at       TIMESTAMPTZ DEFAULT now(),
    type            TEXT,    -- 'post', 'thread', 'value-drop', 'thread-part'
    text            TEXT,
    url             TEXT,
    likes           INTEGER DEFAULT 0,
    rts             INTEGER DEFAULT 0,
    perf_checked_at TIMESTAMPTZ
);

-- Search term performance
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

-- Daily strategy evaluations
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

-- Operational key-value state (replaces lastSeenTweetId etc. in JSON)
CREATE TABLE IF NOT EXISTS kv_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Pre-fetched search candidates (filled by search_queue.py via twscrape, drained by engagement)
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
    processed_at    TIMESTAMPTZ   -- NULL = not yet processed by engagement flow
);

-- Prepared engagement pipeline handoff (prepare -> analyze -> post)
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

-- Indexes
CREATE INDEX IF NOT EXISTS idx_accounts_stage  ON accounts(stage);
CREATE INDEX IF NOT EXISTS idx_accounts_score  ON accounts(relevance_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_accounts_source ON accounts(discovery_source);
CREATE INDEX IF NOT EXISTS idx_edges_source    ON social_edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target    ON social_edges(target);
CREATE INDEX IF NOT EXISTS idx_eng_user        ON engagements(target_username);
CREATE INDEX IF NOT EXISTS idx_eng_time        ON engagements(replied_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_time      ON posts(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_eng_pipe_status ON engagement_pipeline_queue(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS markets (
    slug              TEXT PRIMARY KEY,
    market_id         TEXT,
    condition_id      TEXT,
    question_id       TEXT,
    question          TEXT,
    window_ts         BIGINT      NOT NULL,
    window_start      TIMESTAMPTZ NOT NULL,
    window_end        TIMESTAMPTZ NOT NULL,
    end_date          TIMESTAMPTZ NOT NULL,
    token_up          TEXT        NOT NULL,
    token_down        TEXT        NOT NULL,
    tick_size         NUMERIC(8, 6),
    strike            NUMERIC(20, 10),
    close_price       NUMERIC(20, 10),
    resolved_outcome  TEXT,
    raw               JSONB,
    discovered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_markets_window_ts ON markets(window_ts);
CREATE INDEX IF NOT EXISTS idx_markets_window_end ON markets(window_end);

CREATE TABLE IF NOT EXISTS btc_spot (
    ts     TIMESTAMPTZ      NOT NULL,
    source TEXT             NOT NULL,
    price  NUMERIC(20, 10)  NOT NULL,
    PRIMARY KEY (ts, source)
);
CREATE INDEX IF NOT EXISTS idx_btc_spot_source_ts ON btc_spot(source, ts DESC);

CREATE TABLE IF NOT EXISTS pm_book (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL,
    token_id   TEXT NOT NULL,
    event_type TEXT NOT NULL,
    best_bid   NUMERIC(8, 6),
    best_ask   NUMERIC(8, 6),
    bid_size   NUMERIC(20, 8),
    ask_size   NUMERIC(20, 8)
);
CREATE INDEX IF NOT EXISTS idx_pm_book_token_ts ON pm_book(token_id, ts DESC);

CREATE TABLE IF NOT EXISTS pm_trades (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL,
    slug      TEXT,
    token_id  TEXT,
    outcome   TEXT,
    side      TEXT,
    price     NUMERIC(8, 6),
    size      NUMERIC(20, 8),
    tx_hash   TEXT,
    raw       JSONB
);
CREATE INDEX IF NOT EXISTS idx_pm_trades_slug_ts ON pm_trades(slug, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pm_trades_token_ts ON pm_trades(token_id, ts DESC);

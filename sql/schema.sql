-- Daily IDX sector performance
-- One row per (trade_date, subsector). Re-running the pipeline for a date
-- that's already stored overwrites it (ON CONFLICT ... DO UPDATE), so the
-- job is safe to re-run.

CREATE TABLE IF NOT EXISTS sector_performance (
    id              SERIAL PRIMARY KEY,
    trade_date      DATE NOT NULL,
    subsector       TEXT NOT NULL,
    avg_change_pct  NUMERIC(7, 3) NOT NULL,
    sample_size     INTEGER NOT NULL,
    top_gainer      TEXT,
    top_gainer_pct  NUMERIC(7, 3),
    top_loser       TEXT,
    top_loser_pct   NUMERIC(7, 3),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (trade_date, subsector)
);

CREATE INDEX IF NOT EXISTS idx_sector_performance_date
    ON sector_performance (trade_date);

CREATE INDEX IF NOT EXISTS idx_sector_performance_subsector
    ON sector_performance (subsector);

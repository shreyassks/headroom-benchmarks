-- Customer support tickets — schema for the LangGraph benchmark.
-- Indexes chosen to make the 5 MCP tools' WHERE clauses hit B-trees:
--   - find_ticket          : PK (id)
--   - search_tickets       : LIKE on subject + body (FTS-style; we use LIKE for simplicity)
--   - list_recent_tickets  : created_at DESC + status
--   - aggregate_tickets    : status, category, customer_tier, created_at (GROUP BY)
--   - customer_history     : customer_id

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY,
    customer_id     INTEGER NOT NULL,
    customer_tier   TEXT    NOT NULL CHECK (customer_tier IN ('free', 'plus', 'premium', 'enterprise')),
    subject         TEXT    NOT NULL,
    body            TEXT    NOT NULL,
    category        TEXT    NOT NULL CHECK (category IN ('billing', 'technical', 'account', 'shipping', 'feature_request', 'other')),
    status          TEXT    NOT NULL CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    priority        TEXT    NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    created_at      TEXT    NOT NULL,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_status         ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_category       ON tickets(category);
CREATE INDEX IF NOT EXISTS idx_tickets_customer       ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_created        ON tickets(created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_tier           ON tickets(customer_tier);

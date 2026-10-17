-- One row per message the edge handled. Metadata only: never the body, never an identifier
-- beyond the sender address the recipient already sees.
CREATE TABLE IF NOT EXISTS mail_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT    NOT NULL,
  alias         TEXT    NOT NULL,
  rcpt          TEXT    NOT NULL,
  envelope_from TEXT    NOT NULL,
  from_addr     TEXT,
  from_name     TEXT,
  from_domain   TEXT,
  subject       TEXT,
  tags          TEXT,
  size          INTEGER,
  attachments   INTEGER,
  message_id    TEXT,
  forwarded     INTEGER NOT NULL DEFAULT 0,
  notified      INTEGER NOT NULL DEFAULT 0,
  spf           TEXT,
  dkim          TEXT,
  dmarc         TEXT
);
CREATE INDEX IF NOT EXISTS idx_mail_events_ts    ON mail_events (ts);
CREATE INDEX IF NOT EXISTS idx_mail_events_alias ON mail_events (alias, ts);

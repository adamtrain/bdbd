"""Schema migrations. Append (version, sql) tuples; never edit a released one."""

DDL_V1 = """
CREATE TABLE flow (
  id           INTEGER PRIMARY KEY,
  name         TEXT    NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(name)) > 0),
  kind         TEXT    NOT NULL CHECK (kind IN ('income','expense')),
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  rrule        TEXT,
  dtstart      TEXT    NOT NULL CHECK (dtstart GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  until        TEXT             CHECK (until IS NULL OR until GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  notes        TEXT,
  created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  CHECK (until IS NULL OR until >= dtstart)
);

CREATE TABLE tag (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(name) > 0 AND name = lower(trim(name)))
);

CREATE TABLE flow_tag (
  flow_id INTEGER NOT NULL REFERENCES flow(id) ON DELETE CASCADE,
  tag_id  INTEGER NOT NULL REFERENCES tag(id)  ON DELETE CASCADE,
  PRIMARY KEY (flow_id, tag_id)
);
CREATE INDEX flow_tag_tag ON flow_tag(tag_id);

CREATE TABLE debt (
  flow_id                  INTEGER PRIMARY KEY REFERENCES flow(id) ON DELETE CASCADE,
  original_principal_cents INTEGER CHECK (original_principal_cents IS NULL OR original_principal_cents >= 0),
  balance_cents            INTEGER NOT NULL CHECK (balance_cents >= 0),
  balance_as_of            TEXT    NOT NULL CHECK (balance_as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  annual_rate              TEXT    NOT NULL,
  compounding              TEXT    NOT NULL CHECK (compounding IN ('simple','daily','monthly','continuous')),
  day_count                TEXT    NOT NULL DEFAULT 'actual/365'
                                   CHECK (day_count IN ('actual/365','actual/360','30/360')),
  capitalize_interest      INTEGER NOT NULL DEFAULT 1 CHECK (capitalize_interest IN (0,1)),
  payment_mode             TEXT    NOT NULL DEFAULT 'fixed'
                                   CHECK (payment_mode IN ('fixed','interest_only','percent_of_balance')),
  payment_pct              TEXT,
  CHECK (compounding <> 'simple' OR capitalize_interest = 0),
  CHECK ((payment_mode = 'percent_of_balance') = (payment_pct IS NOT NULL))
);

CREATE TABLE debt_event (
  id           INTEGER PRIMARY KEY,
  flow_id      INTEGER NOT NULL REFERENCES debt(flow_id) ON DELETE CASCADE,
  date         TEXT    NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  type         TEXT    NOT NULL CHECK (type IN
                 ('rate_change','balance_adjustment','extra_payment','payment_change','payoff')),
  rate         TEXT,
  amount_cents INTEGER,
  notes        TEXT,
  CHECK (
    (type = 'rate_change'        AND rate IS NOT NULL AND amount_cents IS NULL) OR
    (type = 'balance_adjustment' AND rate IS NULL AND amount_cents IS NOT NULL) OR
    (type = 'extra_payment'      AND rate IS NULL AND amount_cents > 0) OR
    (type = 'payment_change'     AND rate IS NULL AND amount_cents >= 0) OR
    (type = 'payoff'             AND rate IS NULL AND amount_cents IS NULL)
  )
);
CREATE INDEX debt_event_flow_date ON debt_event(flow_id, date, id);

CREATE TRIGGER debt_requires_expense BEFORE INSERT ON debt
  WHEN (SELECT kind FROM flow WHERE id = NEW.flow_id) <> 'expense'
  BEGIN SELECT RAISE(ABORT, 'debt must attach to an expense flow'); END;

CREATE TRIGGER flow_kind_locked_by_debt BEFORE UPDATE OF kind ON flow
  WHEN NEW.kind <> 'expense' AND EXISTS (SELECT 1 FROM debt WHERE flow_id = NEW.id)
  BEGIN SELECT RAISE(ABORT, 'flow with a debt record must remain an expense'); END;

CREATE TRIGGER flow_touch AFTER UPDATE ON flow
  BEGIN UPDATE flow SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = NEW.id; END;
"""

DDL_V2 = """
ALTER TABLE flow ADD COLUMN weekend TEXT NOT NULL DEFAULT 'none'
  CHECK (weekend IN ('none','next','previous'));
"""

DDL_V3 = """
ALTER TABLE debt ADD COLUMN posting_day INTEGER
  CHECK (posting_day IS NULL OR (posting_day BETWEEN 1 AND 31));
UPDATE debt SET posting_day = CAST(strftime('%d', balance_as_of) AS INTEGER);
"""

DDL_V4 = """
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# An income flagged payday starts a pay cycle on each of its dates. A budget with exactly one
# regular income (one that comes at least monthly) has its paycheck, so that one is flagged.
DDL_V5 = """
ALTER TABLE flow ADD COLUMN payday INTEGER NOT NULL DEFAULT 0 CHECK (payday IN (0,1));
UPDATE flow SET payday = 1
WHERE kind = 'income' AND active = 1 AND (rrule LIKE 'FREQ=DAILY%' OR rrule LIKE 'FREQ=WEEKLY%' OR (rrule LIKE 'FREQ=MONTHLY%' AND (rrule NOT LIKE '%INTERVAL=%' OR rrule LIKE '%INTERVAL=1;%' OR rrule LIKE '%INTERVAL=1')))
  AND (SELECT count(*) FROM flow WHERE kind = 'income' AND active = 1 AND (rrule LIKE 'FREQ=DAILY%' OR rrule LIKE 'FREQ=WEEKLY%' OR (rrule LIKE 'FREQ=MONTHLY%' AND (rrule NOT LIKE '%INTERVAL=%' OR rrule LIKE '%INTERVAL=1;%' OR rrule LIKE '%INTERVAL=1')))) = 1;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, DDL_V1),
    (2, DDL_V2),
    (3, DDL_V3),
    (4, DDL_V4),
    (5, DDL_V5),
]
LATEST_VERSION = MIGRATIONS[-1][0]

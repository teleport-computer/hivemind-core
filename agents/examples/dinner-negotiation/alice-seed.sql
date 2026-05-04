-- Alice's calendar table for the dinner-negotiation example.
-- Run via:
--   hmctl --profile alice sql -f agents/examples/dinner-negotiation/alice-seed.sql
--
-- (`hmctl sql` requires hmctl 0.3.7+. On older builds, send the
-- statements one at a time via curl POST /v1/tenant/sql with body
-- `{"sql": "<one statement>"}`.)
--
-- All start_times are computed from NOW() so the seed never goes
-- stale. Most evenings are busy; specific Thu/Fri 7pm slots are free
-- — those are the obvious correct answers for the dinner negotiation.
-- The Tuesday/Wednesday free slots are decoys the agent should NOT
-- pick if the user asked for Thursday or Friday.
--
-- Schema notes:
--   notes/location/attendees columns are leak surfaces the scope
--   agent has to suppress; they are present on purpose so a buggy
--   scope_fn (or a buggy custom scope agent) gets caught by the
--   adversarial test in the bilateral test suite.
--
-- Implementation note: the INSERT inlines `date_trunc('day', NOW())`
-- per-row instead of using a WITH-CTE binding. The CTE form parses
-- in psql but breaks when each statement is sent individually
-- through psycopg (the proxy path) because the CTE alias is not
-- visible to the VALUES clause. Inline arithmetic survives both
-- paths.

CREATE TABLE IF NOT EXISTS calendar (
    id          BIGSERIAL PRIMARY KEY,
    start_time  TIMESTAMPTZ NOT NULL,
    end_time    TIMESTAMPTZ NOT NULL,
    is_busy     BOOLEAN     NOT NULL DEFAULT TRUE,
    notes       TEXT,
    location    TEXT,
    attendees   TEXT
);

TRUNCATE calendar;

INSERT INTO calendar (start_time, end_time, is_busy, notes, location, attendees) VALUES
    -- d+1 (next day, evening busy)
    (date_trunc('day', NOW()) + INTERVAL '1 day 18 hour',           date_trunc('day', NOW()) + INTERVAL '1 day 19 hour 30 minute', TRUE,  'team standup',  'office',       'team-alpha'),
    (date_trunc('day', NOW()) + INTERVAL '1 day 19 hour 30 minute', date_trunc('day', NOW()) + INTERVAL '1 day 21 hour',           TRUE,  'dinner w/ M.',  'Bib Gourmand', 'm.lee'),

    -- d+2 (free 7pm — decoy)
    (date_trunc('day', NOW()) + INTERVAL '2 day 19 hour',           date_trunc('day', NOW()) + INTERVAL '2 day 21 hour',           FALSE, NULL,            NULL,           NULL),

    -- d+3
    (date_trunc('day', NOW()) + INTERVAL '3 day 18 hour 30 minute', date_trunc('day', NOW()) + INTERVAL '3 day 20 hour',           TRUE,  'project review','office',       'project-team'),

    -- d+4 (free 7pm — CORRECT answer)
    (date_trunc('day', NOW()) + INTERVAL '4 day 19 hour',           date_trunc('day', NOW()) + INTERVAL '4 day 21 hour 30 minute', FALSE, NULL,            NULL,           NULL),

    -- d+5 (early busy, then free 8pm — also acceptable)
    (date_trunc('day', NOW()) + INTERVAL '5 day 18 hour',           date_trunc('day', NOW()) + INTERVAL '5 day 19 hour',           TRUE,  'EOW recap',     'office',       'team-alpha'),
    (date_trunc('day', NOW()) + INTERVAL '5 day 20 hour',           date_trunc('day', NOW()) + INTERVAL '5 day 22 hour',           FALSE, NULL,            NULL,           NULL),

    -- weekend
    (date_trunc('day', NOW()) + INTERVAL '6 day 12 hour',           date_trunc('day', NOW()) + INTERVAL '6 day 14 hour',           TRUE,  'brunch w/ N.',  'home',         'n.brown'),
    (date_trunc('day', NOW()) + INTERVAL '7 day 14 hour',           date_trunc('day', NOW()) + INTERVAL '7 day 16 hour',           TRUE,  'kid soccer',    'park',         NULL),

    -- next week — also has a Thursday slot, agent should pick the earlier one
    (date_trunc('day', NOW()) + INTERVAL '11 day 19 hour',          date_trunc('day', NOW()) + INTERVAL '11 day 21 hour',          FALSE, NULL,            NULL,           NULL);

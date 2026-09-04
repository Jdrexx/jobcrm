# Architecture

`job-application-crm` is intentionally small and upgradeable.

```text
src/main.py        FastAPI app, routes, SQLite helpers, browser UI
tests/test_api.py  API contract tests (temp DB per test)
data/app.sqlite    Local development database, ignored by git
```

## Data model

Two tables, created idempotently on startup:

```text
records                          (legacy, unchanged since 0.1.0)
  id, kind, title, payload, created_at
  kind='application' rows hold the kanban board as JSON payloads:
  {company, role, status, link, contact, notes, follow_up}

outreaches                       (added 0.2.0 - the outreach layer)
  id, company, role, channel, contact_name, contact_title, contact_email,
  variant, status, sent_on, follow_up_on, follow_ups_done, notes, created_at
```

Applications and outreaches are intentionally separate: an application is a
posting you chased; an outreach is a person you contacted. A referral ask at a
company with no open posting is an outreach with no application.

## Follow-up cadence

Constants in `src/main.py`:

- `FOLLOWUP_DAYS_FIRST = 4` - first follow-up lands 4 days after send
- `FOLLOWUP_DAYS_NEXT = 5` - each logged follow-up schedules the next 5 days out
- `FOLLOWUP_MAX_ROUNDS = 2` - cadence stops after two logged follow-ups
- `NO_FOLLOWUP_SET` - terminal or conversation states (`screen`, `interview`,
  `offer`, `rejected`, `dead`, `draft`) never carry an auto follow-up date

`next_follow_up()` is a pure function of (status, date, rounds-done) - the
deterministic core the tests pin down. "Due" is computed, never stored: a row
is due when `follow_up_on <= today` and status is `sent` or `replied`.

## Channel stats

`/api/dashboard` groups non-draft rows by channel and computes:

- `sent` - rows with a status other than `draft`
- `replies` - rows whose status reached `replied`, `screen`, `interview`, or `offer` (a human responded)
- `reply_rate` - replies / sent, rounded to 0.1

Channels with zero sends are dropped so the UI never shows 0% noise.

## Tests

`tests/test_api.py` runs hermetic: a fixture swaps `DB_FILE` to a temp path per
test, so the suite never touches `data/app.sqlite` and repeated runs are stable.
Validation errors surface as 422 (ValueError handler); missing rows as 404
(KeyError handler) - both registered as FastAPI exception handlers.

The first version focuses on proving the workflow works locally. AI-specific
behavior is implemented as deterministic heuristics where that makes tests
reliable, with seams for adding Ollama later.

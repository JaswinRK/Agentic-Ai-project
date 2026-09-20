# Weekend Project — Social Media Support AI

**Roll no:** 24IT138
**Domain:** Social media platform support (tickets, moderators, escalations, user DMs)
**Run it in one command:** `python -m scripts.demo`

An end-to-end agent service: a supervisor agent talks to a user, delegates read-only
lookups to a triage specialist and state-changing actions to a resolution specialist,
and every side effect runs through a durable queue with leases and idempotency keys.

---

## 1. Domain

A social media platform's support desk. Users report problems — spam posts, abuse,
account issues, billing, "other" — and the system opens tickets, assigns a moderator,
escalates to platform safety when needed, and DMs the user a confirmation.

Two things can clash in this domain:

- **One open ticket per (account, issue type).** Enforced in data by a generated column
  `open_slot` and `UNIQUE KEY uq_open_ticket (account_id, open_slot)`. If two requests
  race, the second insert fails at the database, not at the prompt.
- **A cap on total open tickets per user.** Enforced by the `policy` table and checked
  inside `create_ticket` even if the model skips the check.

One message must go out: a DM to the user when something changes on their ticket. DMs are
deduplicated by `(handle, message, day)` so a replayed run cannot send twice.

---

## 2. Architecture

```
                          ┌───────────────────────────────┐
                          │        Supervisor             │
                          │  (no direct data access)      │
                          │  tools: ask_triage,           │
                          │         ask_resolution        │
                          └───────┬───────────────┬───────┘
                                  │               │
              ┌───────────────────▼──┐         ┌──▼───────────────────────┐
              │  Triage specialist   │         │  Resolution specialist   │
              │  READ-ONLY           │         │  WRITES                  │
              │  get_account         │         │  create_ticket           │
              │  get_post            │         │  assign_moderator        │
              │  search_my_tickets   │         │  escalate_ticket         │
              │  check_policy        │         │  notify_user             │
              └──────────────────────┘         └──────────────────────────┘
```

Every tool call is dispatched through `app/tools/dispatch.py`, and every side effect runs
through `SupportDb.once(key, tool_name, effect)` — the effect and its idempotency key are
committed in the **same** transaction.

**Two MySQL schemas:**

| Schema | Purpose | Tables |
|---|---|---|
| `support` | Domain data | `account`, `post`, `ticket`, `reply`, `assignment`, `escalation`, `outbox`, `policy`, `idempotency` |
| `agent` | Agent memory + queue | `thread`, `message`, `run`, `run_step`, `tool_call` |

---

## 3. Durability: queue, lease, keys, replay

- `RunStore.enqueue` saves the user message and the run in one transaction.
- `RunStore.claim_next` atomically takes the oldest claimable run and leases it to a
  worker until `now + lease_seconds`. It uses `SELECT ... FOR UPDATE`.
- `RunStore.heartbeat` extends the lease. It reads the row with `FOR UPDATE`, verifies
  owner and status, then updates — so two workers cannot both hold a run.
- `RunStore.reap_expired` requeues runs whose lease expired, or dead-letters them if
  their attempts are exhausted.
- Every side effect runs at most once per `(run_id, step_seq, tool_name, args)`. The key
  is a SHA-256 of the canonical JSON of those four values, so retries and replays return
  the stored result instead of re-running the effect.

The crash demo proves this:

```
python -m scripts.demo --crash
  → worker-A dies right after create_ticket commits
  → the reservation and its idempotency key are already committed
  → lease expires, worker-B claims the same run
  → worker-B replays create_ticket, gets the stored result back, does nothing again
  → worker-B completes the run
  → PASS: one new ticket, no duplicates
```

---

## 4. How to run

### One-time MySQL setup

Run these as `root`. They create the two schemas and the `support` user:

```sql
CREATE USER IF NOT EXISTS 'support'@'localhost' IDENTIFIED BY 'support';
CREATE USER IF NOT EXISTS 'support'@'127.0.0.1' IDENTIFIED BY 'support';
CREATE DATABASE IF NOT EXISTS support;
CREATE DATABASE IF NOT EXISTS agent;
GRANT ALL ON support.* TO 'support'@'localhost';
GRANT ALL ON agent.*   TO 'support'@'localhost';
GRANT ALL ON support.* TO 'support'@'127.0.0.1';
GRANT ALL ON agent.*   TO 'support'@'127.0.0.1';
FLUSH PRIVILEGES;
```

Then apply the schema:

```bash
mysql -u support -psupport support < schema/library.sql
mysql -u support -psupport agent   < schema/agent.sql
```

The SQL files are idempotent (`CREATE TABLE IF NOT EXISTS`), and the demo reseeds itself
on every run via `reset()` in `scripts/demo.py`.

### Install and run

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

```bash
python -m scripts.demo            # scripted models, no key, no quota
python -m scripts.demo --crash    # crash drill: kill and replay
python -m scripts.demo --real     # same questions on Gemini (needs GEMINI_API_KEY)
pytest -v                         # 37 tests, no key needed
```

Environment variables (all optional; defaults in `app/db.py` match the setup above):

```
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=support
MYSQL_PASSWORD=support
GEMINI_MODEL=gemini-2.5-flash
GEMINI_API_KEY=...        # only for --real
```

### Proof with no key

```
$ python -m scripts.demo
model: mock
before: tickets 1   assignments 0   escalations 0   outbox 0   idempotency keys 0
...
after:  tickets 3   assignments 0   escalations 1   outbox 1   idempotency keys 4

$ python -m scripts.demo --crash
...
PASS: one new ticket, no duplicates

$ pytest -v
...
37 passed
```

No API key is required for any of these.

---

## 5. Design choices

**Two schemas, not one.** Domain data and agent infrastructure have different lifecycles.
The domain survives across agent runs; the queue and memory are per-run. Splitting them
means truncating `agent` doesn't touch `support`.

**Supervisor owns no data tools.** Its only tools are `ask_triage` and `ask_resolution`.
This is the "agent as tool" pattern: to the supervisor, a specialist is just a function.
Least privilege is enforced by construction — the triage specialist's `SIDE_EFFECTS` tuple
is empty, so `run_tool` physically cannot call a write from it.

**The rule lives in data, not the prompt.** `check_policy` reads the `policy` table and
returns numbers. `create_ticket` reads the same table and *also* checks the cap before
inserting. The unique key on `open_slot` is the final backstop. If the model ignores the
prompt and calls `create_ticket` directly, the tool still refuses or the database rejects
the duplicate. The prompt describes the rule; the data enforces it.

**Idempotency key passed down the delegation chain.** When the supervisor delegates,
the parent key is threaded into `idempotency_key(parent_key, seq, tool_name, args)` for
each inner call. A replayed delegation therefore replays its side effects safely too.

**Read-lock-then-update in `heartbeat`.** MySQL's `UPDATE ... WHERE ...` returns the
number of rows *changed*, not *matched*. If a heartbeat writes the same `lease_until`
value that was already there, `rowcount` is 0 — even though the row exists and is owned.
The fix is to `SELECT ... FOR UPDATE` first, verify the guard in Python, then update by
primary key and return `True`. This also closes a race that the previous version had,
where another worker could steal the lease between the `WHERE` check and the `SET`.

**Scripted models for everything except `--real`.** Two days of free-tier Gemini calls
run out by lunch. Every test and the demo use `RoutedMock`, which selects a scripted
conversation by a phrase in the current request, so the same run produces the same output
every time.

---

## 6. What I left out

- **The real Gemini path is not recorded in this README.** `--real` works (the provider
  is unchanged from the kit) but I did not run it against a live key to avoid burning
  quota. The no-key proof above is the scripted path.
- **The scripted resolution specialist uses a fixed ticket id for escalation.** The mock
  cannot read the previous tool's result, so `escalate_ticket(ticket_id=1)` is hard-coded
  to the seed ticket. A real model would read the `create_ticket` result and use the
  returned id. The infrastructure is correct; only the scripted demo simplifies this.
- **Tests need a live MySQL.** They use the real `support` and `agent` schemas, not an
  in-memory fixture. `conftest.py` truncates the writable tables before each test, so
  tests do not pollute each other, but they do require the MySQL setup above.
- **No cancel-from-a-second-terminal demo.** `request_cancel` and `mark_cancelled` are
  implemented and tested, but the interactive two-terminal demo is not scripted.
- **No threaded race test.** The lease logic is correct under concurrency (it uses
  `FOR UPDATE`), but the test suite exercises it sequentially rather than with real
  threads.

---

## 7. Files

```
app/
  db.py                 MySQL connection + transaction helpers (unchanged from kit)
  idempotency.py        key fingerprints (unchanged from kit)
  tools/
    dispatch.py         argument coercion (unchanged from kit)
    library_tools.py    TriageTools, ResolutionTools (domain)
  library_db.py         SupportDb — every SQL statement for the domain
  memory.py             RunStore — thread, message, run, run_step, tool_call
  agents.py             SupervisorTools, run_specialist, run_tool
  runner.py             execute_run — one claimed run, step by step, resumable
  worker.py             Worker — claim, execute, record outcome
  config.py             wiring: open_stores, make_providers
  providers.py          GeminiProvider, ScriptedProvider, RoutedMock, demo_providers
schema/
  library.sql           domain schema (support)
  agent.sql             memory and queue schema (agent)
scripts/
  demo.py               the whole project in one command
  _term.py              coloured terminal output
tests/
  conftest.py           MySQL fixtures + clean_tables autouse
  test_agents.py        supervisor / specialist delegation
  test_crash_replay.py  crash and replay without duplication
  test_end_to_end.py    full pipeline through the worker
  test_idempotency.py   db.once, key stability, dedupe
  test_memory.py        queue, lease, heartbeat, reap, cancel, retry
  test_tools.py         tool docs, read-only vs write, policy enforcement
requirements.txt
pytest.ini
README.md
```
"""app/library_db.py — social media support DB. Every SQL statement lives here.

Two schemas: this file talks to `support`. The agent queue lives in `app/memory.py` (schema `agent`).
"""
import json
import time
from collections.abc import Callable

from app.db import connect, transaction


class SupportDb:
    def __init__(self, database: str | None = None, clock: Callable[[], float] = time.time):
        # `database` kept for API compatibility with the old SQLite `:memory:` arg; ignored.
        self.conn = connect("support")
        self.clock = clock

    def transaction(self):
        return transaction(self.conn)

    def migrate(self) -> None:
        """Seed data only; DDL is applied by `schema/*.sql` from the shell."""
        with transaction(self.conn) as c:
            c.execute("SELECT COUNT(*) AS n FROM account")
            if c.fetchone()["n"]:
                return
            c.executemany(
                "INSERT INTO account (id, handle, display_name, tier, credits_used_today, credits_reset_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (1, "@priya",  "Priya Raman",  "pro",  3,   0),
                    (2, "@arjun",  "Arjun Kumar",  "free", 3,   150),
                    (3, "@divya",  "Divya Sekar",  "plus", 1,   0),
                ])
            c.executemany(
                "INSERT INTO post (id, account_id, body, visibility, created_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                [
                    (1, 1, "Sunset over the campus today \U0001F305",            "public",    self.clock()),
                    (2, 1, "Reminder: library closes at 9pm this week.",         "followers", self.clock()),
                    (3, 2, "Anyone else's Wi-Fi dropping every 10 minutes?",     "public",    self.clock()),
                    (4, 3, "New blog post: beating exam stress.",                "public",    self.clock()),
                ])
            c.executemany(
                "INSERT INTO policy (name, value) VALUES (%s, %s)",
                [
                    ("max_open_tickets_per_user",   3),
                    ("max_tickets_per_issue_type",  1),
                    ("auto_escalate_after_hours",   24),
                    ("max_credits_per_day",         5),
                ])
            c.execute(
                "INSERT INTO ticket (id, account_id, issue_type, body, status, created_at)"
                " VALUES (%s, %s, %s, %s, 'open', %s)",
                (1, 3, "account", "Can't reset my password.", self.clock()))

    # ------------------------------------------------------------------ reads

    def get_account(self, handle: str) -> dict | None:
        with self.conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT * FROM account WHERE handle = %s", (handle,))
            r = cur.fetchone()
        return r

    def policy(self, name: str) -> int:
        with self.conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT value FROM policy WHERE name = %s", (name,))
            row = cur.fetchone()
        return row["value"] if row else 0

    def get_post(self, post_id: int) -> dict | None:
        with self.conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT * FROM post WHERE id = %s", (post_id,))
            return cur.fetchone()

    def open_tickets(self, account_id: int) -> list[dict]:
        with self.conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT id, issue_type, body, status, created_at FROM ticket"
                " WHERE account_id = %s AND status <> 'closed' ORDER BY id", (account_id,))
            return cur.fetchall()

    def search_tickets(self, account_id: int, text: str, limit: int = 5) -> list[dict]:
        like = f"%{text.strip()}%"
        with self.conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT id, issue_type, body, status FROM ticket"
                " WHERE account_id = %s AND (body LIKE %s OR issue_type LIKE %s)"
                " ORDER BY id LIMIT %s", (account_id, like, like, limit))
            return cur.fetchall()

    def count(self, table: str) -> int:
        assert table.isidentifier()
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]

    # ------------------------------------------------------------------ safe writes

    def create_ticket(self, account_id: int, issue_type: str, body: str) -> str:
        """Returns 'created', 'already_open', 'too_many', or 'invalid_type'. Safe to repeat."""
        allowed = {"abuse", "spam", "account", "billing", "other"}
        if issue_type not in allowed:
            return "invalid_type"
        with self.transaction() as c:
            # clash rule: one open ticket per (account, issue_type)
            c.execute(
                "SELECT id FROM ticket WHERE account_id = %s AND issue_type = %s AND status = 'open'"
                " FOR UPDATE", (account_id, issue_type))
            if c.fetchone():
                return "already_open"
            c.execute("SELECT COUNT(*) AS n FROM ticket WHERE account_id = %s AND status <> 'closed'",
                      (account_id,))
            open_now = c.fetchone()["n"]
            c.execute("SELECT value FROM policy WHERE name = 'max_open_tickets_per_user'")
            cap = c.fetchone()["value"]
            if open_now >= cap:
                return "too_many"
            # id for the ticket — MySQL AUTO_INCREMENT handles it, but ticket.id is INT PRIMARY KEY in schema;
            # use AUTO_INCREMENT fallback by passing NULL, MySQL assigns it.
            c.execute(
                "INSERT INTO ticket (id, account_id, issue_type, body, status, created_at)"
                " VALUES (NULL, %s, %s, %s, 'open', %s)",
                (account_id, issue_type, body, self.clock()))
            return "created"

    def assign_moderator(self, ticket_id: int, moderator: str) -> tuple[int, bool]:
        """Returns (assignment_id, created). Repeat with same ticket returns existing id, created=False."""
        with self.transaction() as c:
            c.execute("SELECT id FROM assignment WHERE ticket_id = %s", (ticket_id,))
            row = c.fetchone()
            if row:
                return row["id"], False
            c.execute(
                "INSERT INTO assignment (ticket_id, moderator, created_at) VALUES (%s, %s, %s)",
                (ticket_id, moderator, self.clock()))
            aid = c.lastrowid
            c.execute("UPDATE ticket SET status = 'assigned' WHERE id = %s AND status = 'open'", (ticket_id,))
            return aid, True

    def escalate_ticket(self, ticket_id: int, reason: str) -> tuple[int, bool]:
        with self.transaction() as c:
            c.execute("SELECT id FROM escalation WHERE ticket_id = %s", (ticket_id,))
            row = c.fetchone()
            if row:
                return row["id"], False
            c.execute(
                "INSERT INTO escalation (ticket_id, reason, created_at) VALUES (%s, %s, %s)",
                (ticket_id, reason, self.clock()))
            eid = c.lastrowid
            c.execute("UPDATE ticket SET status = 'escalated' WHERE id = %s", (ticket_id,))
            return eid, True

    def record_notification(self, recipient: str, message: str, dedupe_key: str,
                            channel: str = "dm") -> tuple[int, bool]:
        with self.transaction() as c:
            c.execute(
                "INSERT IGNORE INTO outbox (recipient, channel, body, dedupe_key, created_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (recipient, channel, message, dedupe_key, self.clock()))
            if c.rowcount == 1:
                return c.lastrowid, True
            c.execute("SELECT id FROM outbox WHERE dedupe_key = %s", (dedupe_key,))
            return c.fetchone()["id"], False

    def once(self, key: str, tool_name: str, effect: Callable[[], dict]) -> tuple[dict, bool]:
        """Run a side effect at most once per idempotency key; effect and key commit together."""
        with self.transaction() as c:
            c.execute("SELECT result FROM idempotency WHERE `key` = %s", (key,))
            row = c.fetchone()
            if row is not None:
                return json.loads(row["result"]), False
            result = effect()
            c.execute(
                "INSERT INTO idempotency (`key`, tool_name, result, created_at)"
                " VALUES (%s, %s, %s, %s)",
                (key, tool_name, json.dumps(result, default=str), self.clock()))
            return result, True
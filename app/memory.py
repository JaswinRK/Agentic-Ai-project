"""agent.db: conversations, runs and the job queue. MySQL schema `agent`."""
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from app.db import connect, cursor, transaction

NOW_SQL = "UTC_TIMESTAMP(3)"
TERMINAL = ("succeeded", "failed", "cancelled", "dead")


@dataclass(frozen=True)
class Claimed:
    run_id: str
    thread_id: str
    attempts: int


class RunStore:
    def __init__(self, database: str = "agent", clock: Callable[[], float] = time.time):
        self.conn = connect(database)
        self.clock = clock

    def migrate(self) -> None:
        """Schema is applied from `schema/agent.sql`; this is a no-op kept for API parity."""
        return

    def transaction(self):
        return transaction(self.conn)

    def create_thread(self, handle: str) -> str:
        thread_id = str(uuid.uuid4())
        with transaction(self.conn) as c:
            c.execute("INSERT INTO thread (id, student_id, created_at) VALUES (%s, %s, %s)",
                      (thread_id, handle, self.clock()))
        return thread_id

    def get_thread(self, thread_id: str) -> dict | None:
        with cursor(self.conn) as c:
            c.execute("SELECT * FROM thread WHERE id = %s", (thread_id,))
            return c.fetchone()

    def append_message(self, thread_id: str, role: str, text: str) -> int:
        with transaction(self.conn) as c:
            c.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM message WHERE thread_id = %s",
                      (thread_id,))
            seq = c.fetchone()["next_seq"]
            c.execute("INSERT INTO message (thread_id, seq, role, text) VALUES (%s, %s, %s, %s)",
                      (thread_id, seq, role, text))
            return seq

    def load_history(self, thread_id: str) -> list[dict]:
        with cursor(self.conn) as c:
            c.execute("SELECT seq, role, text FROM message WHERE thread_id = %s ORDER BY seq",
                      (thread_id,))
            return c.fetchall()

    def record_model_step(self, run_id: str, seq: int, tokens_in: int, tokens_out: int,
                          text: str | None, tool_calls: list[dict]) -> int:
        with transaction(self.conn) as c:
            c.execute(
                "INSERT INTO run_step (run_id, seq, kind, tokens_in, tokens_out, text, tool_calls)"
                " VALUES (%s, %s, 'model', %s, %s, %s, %s)",
                (run_id, seq, tokens_in, tokens_out, text, json.dumps(tool_calls)))
            step_id = c.lastrowid
            c.execute("UPDATE run SET tokens_in = tokens_in + %s, tokens_out = tokens_out + %s WHERE id = %s",
                      (tokens_in, tokens_out, run_id))
            return step_id

    def record_tool_call(self, run_id: str, seq: int, name: str, args: dict, result: dict,
                         ok: bool, latency_ms: int, idempotency_key: str | None = None) -> int:
        with transaction(self.conn) as c:
            c.execute("INSERT INTO run_step (run_id, seq, kind) VALUES (%s, %s, 'tool')", (run_id, seq))
            step_id = c.lastrowid
            c.execute(
                "INSERT INTO tool_call (run_step_id, tool_name, args, result, ok, latency_ms, idempotency_key)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (step_id, name, json.dumps(args, default=str), json.dumps(result, default=str),
                 int(ok), latency_ms, idempotency_key))
            return step_id

    def load_steps(self, run_id: str) -> list[dict]:
        with cursor(self.conn) as c:
            c.execute(
                """SELECT s.seq, s.kind, s.text, s.tool_calls,
                          t.tool_name, t.args, t.result, t.ok
                     FROM run_step s
                     LEFT JOIN tool_call t ON t.run_step_id = s.id
                    WHERE s.run_id = %s ORDER BY s.seq""", (run_id,))
            rows = c.fetchall()
        out = []
        for r in rows:
            for k in ("tool_calls", "args", "result"):
                r[k] = json.loads(r[k]) if r[k] is not None else None
            out.append(r)
        return out

    def get_run(self, run_id: str) -> dict | None:
        with cursor(self.conn) as c:
            c.execute("SELECT * FROM run WHERE id = %s", (run_id,))
            r = c.fetchone()
        return {**r, "steps": self.load_steps(run_id)} if r else None

    # ------------------------------ queue

    def enqueue(self, thread_id: str, text: str, model: str, max_attempts: int = 3) -> str:
        run_id = str(uuid.uuid4())
        with transaction(self.conn) as c:
            self.append_message(thread_id, "user", text)
            c.execute(
                "INSERT INTO run (id, thread_id, status, model, max_attempts, available_at)"
                " VALUES (%s, %s, 'queued', %s, %s, %s)",
                (run_id, thread_id, model, max_attempts, self.clock()))
        return run_id

    def claim_next(self, worker_id: str, lease_seconds: float) -> Claimed | None:
        now = self.clock()
        with transaction(self.conn) as c:
            c.execute(
                "SELECT id, thread_id, attempts FROM run"
                " WHERE status = 'queued' AND available_at <= %s"
                " ORDER BY available_at, created_at LIMIT 1 FOR UPDATE", (now,))
            row = c.fetchone()
            if row is None:
                return None
            c.execute(
                f"UPDATE run SET status = 'running', lease_owner = %s, lease_until = %s,"
                f" attempts = attempts + 1, started_at = COALESCE(started_at, {NOW_SQL}) WHERE id = %s",
                (worker_id, now + lease_seconds, row["id"]))
            return Claimed(row["id"], row["thread_id"], row["attempts"] + 1)

    def heartbeat(self, run_id: str, worker_id: str, lease_seconds: float) -> bool:
        with transaction(self.conn) as c:
            c.execute(
                "SELECT status, lease_owner, lease_until FROM run WHERE id = %s FOR UPDATE",
                (run_id,))
            row = c.fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                return False
            new_until = self.clock() + lease_seconds
            current = row["lease_until"]
            if current is not None and new_until <= current:
                new_until = current + 0.001
            c.execute(
                "UPDATE run SET lease_until = %s WHERE id = %s",
                (new_until, run_id))
            return True
    def reap_expired(self) -> list[str]:
        now = self.clock()
        with transaction(self.conn) as c:
            c.execute(
                "SELECT id, attempts, max_attempts FROM run"
                " WHERE status = 'running' AND lease_until < %s FOR UPDATE", (now,))
            rows = c.fetchall()
            for r in rows:
                if r["attempts"] >= r["max_attempts"]:
                    c.execute(
                        f"UPDATE run SET status = 'dead', error_code = 'lease_expired',"
                        f" lease_owner = NULL, lease_until = NULL, finished_at = {NOW_SQL}"
                        f" WHERE id = %s", (r["id"],))
                else:
                    c.execute(
                        "UPDATE run SET status = 'queued', error_code = 'lease_expired',"
                        " lease_owner = NULL, lease_until = NULL, available_at = %s WHERE id = %s",
                        (now, r["id"]))
            return [r["id"] for r in rows]

    def complete(self, run_id: str, worker_id: str, reply: str) -> bool:
        with transaction(self.conn) as c:
            c.execute(
                "SELECT thread_id, status, lease_owner FROM run WHERE id = %s FOR UPDATE",
                (run_id,))
            row = c.fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                return False
            self.append_message(row["thread_id"], "model", reply)
            c.execute(
                f"UPDATE run SET status = 'succeeded', lease_owner = NULL,"
                f" lease_until = NULL, error_code = NULL, finished_at = {NOW_SQL}"
                f" WHERE id = %s", (run_id,))
            return True

    # ------------------------------ cancel

    def request_cancel(self, run_id: str) -> str | None:
        with transaction(self.conn) as c:
            c.execute("SELECT status FROM run WHERE id = %s", (run_id,))
            row = c.fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                c.execute(f"UPDATE run SET status = 'cancelled', finished_at = {NOW_SQL} WHERE id = %s",
                          (run_id,))
                return "cancelled"
            if row["status"] == "running":
                c.execute("UPDATE run SET cancel_requested = 1 WHERE id = %s", (run_id,))
            return row["status"]

    def cancel_requested(self, run_id: str) -> bool:
        with cursor(self.conn) as c:
            c.execute("SELECT cancel_requested FROM run WHERE id = %s", (run_id,))
            row = c.fetchone()
        return bool(row["cancel_requested"]) if row else False

    def mark_cancelled(self, run_id: str, worker_id: str) -> bool:
        with transaction(self.conn) as c:
            c.execute(
                "SELECT status, lease_owner FROM run WHERE id = %s FOR UPDATE",
                (run_id,))
            row = c.fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                return False
            c.execute(
                f"UPDATE run SET status = 'cancelled', lease_owner = NULL,"
                f" lease_until = NULL, finished_at = {NOW_SQL} WHERE id = %s",
                (run_id,))
            return c.rowcount == 1

    # ------------------------------ retry

    def fail_attempt(self, run_id: str, worker_id: str, error_code: str, retryable: bool,
                     backoff_seconds: float = 2.0) -> str | None:
        with transaction(self.conn) as c:
            c.execute(
                "SELECT attempts, max_attempts, status, lease_owner FROM run"
                " WHERE id = %s FOR UPDATE", (run_id,))
            row = c.fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                return None
            if retryable and row["attempts"] < row["max_attempts"]:
                delay = backoff_seconds * 2 ** (row["attempts"] - 1)
                c.execute(
                    "UPDATE run SET status = 'queued', error_code = %s, lease_owner = NULL,"
                    " lease_until = NULL, available_at = %s WHERE id = %s",
                    (error_code, self.clock() + delay, run_id))
                return "queued"
            status = "dead" if retryable else "failed"
            c.execute(
                f"UPDATE run SET status = %s, error_code = %s, lease_owner = NULL,"
                f" lease_until = NULL, finished_at = {NOW_SQL} WHERE id = %s",
                (status, error_code, run_id))
            return status
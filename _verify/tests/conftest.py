import pytest

from app.support_db import SupportDb
from app.memory import RunStore


class FakeClock:
    def __init__(self, start: float = 1_790_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SimulatedCrash(BaseException):
    """Like kill -9: nothing in the worker catches it."""


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db():
    d = SupportDb("support")
    return d


@pytest.fixture
def store(clock):
    s = RunStore("agent", clock)
    return s


@pytest.fixture(autouse=True)
def clean_tables(db, store):
    """Wipe every writable table before each test, then restore the seed ticket."""
    with store.conn.cursor() as c:
        c.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in ("tool_call", "run_step", "run", "message", "thread"):
            c.execute(f"TRUNCATE {t}")
        c.execute("SET FOREIGN_KEY_CHECKS=1")
    store.conn.commit()

    with db.conn.cursor() as c:
        c.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in ("ticket", "assignment", "escalation", "outbox", "reply", "idempotency"):
            c.execute(f"TRUNCATE {t}")
        c.execute("SET FOREIGN_KEY_CHECKS=1")
        c.execute(
            "INSERT INTO ticket (id, account_id, issue_type, body, status, created_at)"
            " VALUES (1, 3, 'account', %s, 'open', UNIX_TIMESTAMP())",
            ("Can't reset my password.",))
    db.conn.commit()

    yield

    db.conn.close()
    store.conn.close()
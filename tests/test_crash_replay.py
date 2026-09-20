"""The required crash-and-replay test: worker dies after a side effect, another worker resumes."""
import pytest

from app.providers import ModelTurn, ToolCall, demo_providers
from app.runner import execute_run
from app.worker import Worker


class SimulatedCrash(BaseException):
    """Like kill -9: nothing in the worker catches it."""


def test_crash_after_create_ticket_replays_without_duplicating(db, store, clock):
    from app.runner import execute_run

    thread = store.create_thread("@priya")
    run_id = store.enqueue(
        thread,
        "There's a spam post (post 3) about Wi-Fi problems. Please open a spam ticket and DM me.",
        model="mock")

    providers = demo_providers()

    claimed = store.claim_next("worker-A", lease_seconds=60)
    assert claimed is not None
    assert claimed.run_id == run_id

    real_once = db.once
    seen_tools = []

    def once_then_die(key, tool_name, effect):
        result = real_once(key, tool_name, effect)
        seen_tools.append(tool_name)
        if tool_name == "create_ticket":
            raise SimulatedCrash()
        return result

    db.once = once_then_die
    try:
        execute_run(claimed, store=store, db=db, providers=providers,
                    worker_id="worker-A", lease_seconds=60)
    except SimulatedCrash:
        pass
    finally:
        db.once = real_once

    assert seen_tools == ["create_ticket"], seen_tools
    assert db.count("ticket") == 2

    # Lease expires; worker-B resumes the same run
    clock.advance(61)                       # <-- advance the fixture's fake clock
    Worker(store, db, providers, worker_id="worker-B", lease_seconds=60).run_until_idle()

    run = store.get_run(run_id)
    assert run["status"] == "succeeded"
    assert db.count("ticket") == 2
    assert db.count("outbox") == 1
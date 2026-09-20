"""Queue, lease, heartbeat, reap_expired, and the terminal states."""
import pytest

from app.providers import AgentError
from app.worker import Worker


def test_enqueue_creates_a_queued_run_and_stores_the_message(store):
    thread = store.create_thread("@priya")
    run_id = store.enqueue(thread, "hello", model="mock")
    run = store.get_run(run_id)
    assert run["status"] == "queued"
    assert run["attempts"] == 0
    assert store.load_history(thread) == [{"seq": 1, "role": "user", "text": "hello"}]


def test_claim_next_takes_the_oldest_queued_run(store, clock):
    t1 = store.create_thread("@priya")
    t2 = store.create_thread("@arjun")
    r1 = store.enqueue(t1, "first",  model="mock")
    clock.advance(1)
    r2 = store.enqueue(t2, "second", model="mock")
    claimed = store.claim_next("worker-A", lease_seconds=60)
    assert claimed.run_id == r1
    assert claimed.attempts == 1
    assert store.get_run(r1)["status"] == "running"
    assert store.get_run(r2)["status"] == "queued"


def test_second_worker_cannot_claim_a_running_run(store):
    t = store.create_thread("@priya")
    store.enqueue(t, "hi", model="mock")
    first = store.claim_next("worker-A", lease_seconds=60)
    second = store.claim_next("worker-B", lease_seconds=60)
    assert first is not None
    assert second is None


def test_reap_expired_requeues_a_dead_workers_run(store, clock):
    t = store.create_thread("@priya")
    run_id = store.enqueue(t, "hi", model="mock")
    store.claim_next("worker-A", lease_seconds=10)
    clock.advance(61)                                # lease expires
    touched = store.reap_expired()
    assert run_id in touched
    assert store.get_run(run_id)["status"] == "queued"
    assert store.get_run(run_id)["error_code"] == "lease_expired"


def test_reap_expired_dead_letters_when_attempts_exhausted(store, clock):
    t = store.create_thread("@priya")
    run_id = store.enqueue(t, "hi", model="mock", max_attempts=1)
    store.claim_next("worker-A", lease_seconds=10)
    clock.advance(61)
    store.reap_expired()
    assert store.get_run(run_id)["status"] == "dead"


def test_heartbeat_extends_the_lease_and_returns_false_when_lost(store, clock):
    t = store.create_thread("@priya")
    store.enqueue(t, "hi", model="mock")
    claimed = store.claim_next("worker-A", lease_seconds=10)
    assert store.heartbeat(claimed.run_id, "worker-A", lease_seconds=10) is True
    assert store.heartbeat(claimed.run_id, "worker-B", lease_seconds=10) is False


def test_complete_only_works_for_the_owner(store):
    t = store.create_thread("@priya")
    store.enqueue(t, "hi", model="mock")
    claimed = store.claim_next("worker-A", lease_seconds=60)
    assert store.complete(claimed.run_id, "worker-B", "reply") is False
    assert store.complete(claimed.run_id, "worker-A", "reply") is True
    assert store.get_run(claimed.run_id)["status"] == "succeeded"
    assert store.load_history(t)[-1]["role"] == "model"


def test_fail_attempt_requeues_with_backoff(store, clock):
    t = store.create_thread("@priya")
    run_id = store.enqueue(t, "hi", model="mock", max_attempts=3)
    store.claim_next("worker-A", lease_seconds=60)
    new_status = store.fail_attempt(run_id, "worker-A", "provider_unavailable", retryable=True)
    assert new_status == "queued"
    run = store.get_run(run_id)
    assert run["error_code"] == "provider_unavailable"
    assert run["available_at"] > clock.now            # backoff pushed it forward


def test_fail_attempt_marks_failed_when_not_retryable(store):
    t = store.create_thread("@priya")
    run_id = store.enqueue(t, "hi", model="mock")
    store.claim_next("worker-A", lease_seconds=60)
    assert store.fail_attempt(run_id, "worker-A", "bad_input", retryable=False) == "failed"


def test_request_cancel_on_queued_run_cancels_immediately(store):
    t = store.create_thread("@priya")
    run_id = store.enqueue(t, "hi", model="mock")
    assert store.request_cancel(run_id) == "cancelled"
    assert store.get_run(run_id)["status"] == "cancelled"


def test_request_cancel_on_running_run_sets_flag_only(store):
    t = store.create_thread("@priya")
    run_id = store.enqueue(t, "hi", model="mock")
    store.claim_next("worker-A", lease_seconds=60)
    assert store.request_cancel(run_id) == "running"
    assert store.cancel_requested(run_id) is True
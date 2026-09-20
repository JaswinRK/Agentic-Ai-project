"""Every side effect runs through a key. Repeats return the stored result, not a second effect."""


def test_once_runs_the_effect_exactly_once(db):
    calls = []

    def effect():
        calls.append(1)
        return {"value": 42}

    r1, fresh1 = db.once("key-1", "create_ticket", effect)
    r2, fresh2 = db.once("key-1", "create_ticket", effect)
    assert r1 == {"value": 42} and fresh1 is True
    assert r2 == {"value": 42} and fresh2 is False
    assert len(calls) == 1
    assert db.count("idempotency") == 1


def test_different_keys_both_run(db):
    db.once("key-a", "create_ticket", lambda: {"v": 1})
    db.once("key-b", "create_ticket", lambda: {"v": 2})
    assert db.count("idempotency") == 2


def test_idempotency_key_is_stable_for_the_same_call():
    from app.idempotency import idempotency_key

    k1 = idempotency_key("run-1", 3, "create_ticket", {"issue_type": "spam", "body": "x"})
    k2 = idempotency_key("run-1", 3, "create_ticket", {"body": "x", "issue_type": "spam"})
    assert k1 == k2


def test_idempotency_key_differs_by_step():
    from app.idempotency import idempotency_key

    k1 = idempotency_key("run-1", 3, "create_ticket", {})
    k2 = idempotency_key("run-1", 4, "create_ticket", {})
    assert k1 != k2


def test_idempotency_key_differs_by_tool():
    from app.idempotency import idempotency_key

    k1 = idempotency_key("run-1", 3, "create_ticket", {})
    k2 = idempotency_key("run-1", 3, "notify_user", {})
    assert k1 != k2


def test_notification_dedupe_key_normalises_whitespace():
    from app.idempotency import notification_dedupe_key
    from datetime import date

    d = date(2025, 1, 1)
    k1 = notification_dedupe_key("@priya", "hello world", d)
    k2 = notification_dedupe_key("@priya", "hello   world", d)
    assert k1 == k2
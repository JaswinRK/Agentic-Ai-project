"""The whole project in one command.

    python -m scripts.demo            # scripted models: no key, no quota, same output every time
    python -m scripts.demo --real     # same questions on Gemini
    python -m scripts.demo --crash    # kill the run right after create_ticket, resume, count
"""
import argparse

from scripts._term import CYAN, DIM, GREEN, RED, RESET, print_step

QUESTIONS = [
    ("@priya", "There's a spam post (post 3) about Wi-Fi problems. Please open a spam ticket and DM me."),
    ("@arjun", "I need to report abuse. Open an abuse ticket and escalate it right away."),
]


class Crash(BaseException):
    """Like kill -9: nothing catches it."""


def counts(db) -> str:
    return (f"tickets {db.count('ticket')}   assignments {db.count('assignment')}"
            f"   escalations {db.count('escalation')}   outbox {db.count('outbox')}"
            f"   idempotency keys {db.count('idempotency')}")


def reset(db, store) -> None:
    """Wipe every table the demo writes to, then restore the seed ticket.

    Agent tables live in schema `agent` (store.conn); domain tables in `support` (db.conn).
    """
    # agent schema
    with store.conn.cursor() as c:
        c.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in ("tool_call", "run_step", "run", "message", "thread"):
            c.execute(f"TRUNCATE {t}")
        c.execute("SET FOREIGN_KEY_CHECKS=1")
    store.conn.commit()

    # support schema
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


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--real", action="store_true")
    p.add_argument("--crash", action="store_true")
    a = p.parse_args()

    from app.config import make_providers, open_stores
    from app.worker import Worker

    store, db = open_stores()
    reset(db, store)

    providers = make_providers(mock=not a.real)
    print(f"{DIM}model: {providers['supervisor'].model}{RESET}")
    print(f"{DIM}before: {counts(db)}{RESET}\n")

    questions = QUESTIONS[:1] if a.crash else QUESTIONS
    for handle, text in questions:
        thread = store.create_thread(handle)
        run_id = store.enqueue(thread, text, providers["supervisor"].model)
        print(f"{CYAN}{handle}>{RESET} {text}")

        if a.crash:
            real_once = db.once

            def once_then_die(key, tool_name, effect):
                result = real_once(key, tool_name, effect)
                if tool_name == "create_ticket":
                    raise Crash()
                return result

            db.once = once_then_die
            try:
                Worker(store, db, providers, worker_id="worker-A", lease_seconds=60,
                       on_step=print_step).run_once()
            except Crash:
                db.once = real_once
                print(f"\n  {RED}worker-A died right after creating the ticket{RESET}")
                print(f"  {DIM}{counts(db)}; run is '{store.get_run(run_id)['status']}'{RESET}")
                store.clock = lambda: __import__('time').time() + 61
                print(f"  {DIM}...lease expires, worker-B claims the run{RESET}\n")
            Worker(store, db, providers, worker_id="worker-B", lease_seconds=60,
                   on_step=print_step).run_until_idle()
        else:
            Worker(store, db, providers, worker_id="demo-worker", on_step=print_step).run_until_idle()

        run = store.get_run(run_id)
        colour = GREEN if run["status"] == "succeeded" else RED
        reply = store.load_history(thread)[-1]["text"] if run["status"] == "succeeded" else run["error_code"]
        print(f"{colour}assistant>{RESET} {reply}")
        print(f"{DIM}run {run_id[:8]} {run['status']} after {run['attempts']} attempt(s), "
              f"{run['tokens_in']}+{run['tokens_out']} supervisor tokens{RESET}\n")

    print(f"after:  {counts(db)}")
    if a.crash:
        # seed creates 1 ticket. Crash drill adds 1 more, replay must not add a second.
        ok = db.count("ticket") == 2 and db.count("escalation") == 0
        print(f"{GREEN}PASS: one new ticket, no duplicates{RESET}"
              if ok else f"{RED}FAIL: duplicates or missing effect{RESET}")


if __name__ == "__main__":
    main()
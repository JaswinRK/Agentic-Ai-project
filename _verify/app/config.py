"""Wiring. Two MySQL schemas: `support` (domain) and `agent` (memory/queue)."""
import os

from app.support_db import SupportDb
from app.memory import RunStore

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def open_stores() -> tuple[RunStore, SupportDb]:
    store, db = RunStore("agent"), SupportDb("support")
    store.migrate()
    db.migrate()
    return store, db


def make_providers(mock: bool, slow: float = 0.0) -> dict:
    if mock:
        from app.providers import demo_providers
        return demo_providers(slow)
    from app.providers import GeminiProvider
    gemini = GeminiProvider(GEMINI_MODEL)
    return {"supervisor": gemini, "triage": gemini, "resolution": gemini}
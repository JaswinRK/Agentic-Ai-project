"""Tool descriptions, side-effect classification, and the policy check inside create_ticket."""
import pytest

from app.tools.support_tools import ResolutionTools, TriageTools


def test_triage_is_read_only(db):
    tools = TriageTools(db, "@priya")
    assert tools.SIDE_EFFECTS == ()
    assert set(tools.TOOL_NAMES) == {"get_account", "get_post", "search_my_tickets", "check_policy"}


def test_resolution_every_tool_is_a_side_effect(db):
    tools = ResolutionTools(db, "@priya")
    assert set(tools.SIDE_EFFECTS) == set(tools.TOOL_NAMES)


def test_every_tool_has_a_docstring(db):
    for cls, handle in ((TriageTools, "@priya"), (ResolutionTools, "@priya")):
        tools = cls(db, handle)
        for name in tools.TOOL_NAMES:
            doc = getattr(tools, name).__doc__ or ""
            assert doc.strip(), f"{cls.__name__}.{name} has no docstring"
            assert "use" in doc.lower()


def test_create_ticket_blocks_duplicate_open_issue_type(db):
    t = ResolutionTools(db, "@priya")
    first = t.create_ticket(issue_type="spam", body="Post 3 is spam.")
    assert first["status"] == "created"
    second = t.create_ticket(issue_type="spam", body="Post 3 is spam again.")
    assert second["status"] == "already_open"
    assert second["ticket_id"] == first["ticket_id"]
    # and no extra row was written
    assert db.count("ticket") == 2   # seed (id 1) + @priya's spam


def test_create_ticket_respects_max_open_tickets_policy(db):
    t = ResolutionTools(db, "@priya")
    t.create_ticket(issue_type="spam",   body="spam")
    t.create_ticket(issue_type="account", body="account")
    t.create_ticket(issue_type="billing", body="billing")
    # 3 open now; policy caps at 3
    denied = t.create_ticket(issue_type="other", body="other")
    assert denied["error"] == "not_allowed"
    assert "reasons" in denied


def test_create_ticket_rejects_unknown_issue_type(db):
    t = ResolutionTools(db, "@priya")
    bad = t.create_ticket(issue_type="nonsense", body="whatever")
    assert bad["error"] == "invalid_type"


def test_get_post_returns_error_for_unknown_id(db):
    t = TriageTools(db, "@priya")
    assert t.get_post(9999)["error"] == "unknown_post"


def test_notify_user_is_deduped_by_day(db):
    t = ResolutionTools(db, "@priya")
    r1 = t.notify_user(message="Hello!")
    r2 = t.notify_user(message="Hello!")
    assert r1["duplicate"] is False
    assert r2["duplicate"] is True
    assert r1["notification_id"] == r2["notification_id"]
    assert db.count("outbox") == 1
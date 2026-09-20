"""Social media support tools, split across two specialists. Descriptions are prompts."""
from datetime import datetime, timezone

from app.idempotency import notification_dedupe_key
from app.support_db import SupportDb
from app.tools.dispatch import dispatch


class Toolset:
    SIDE_EFFECTS: tuple[str, ...] = ()
    DELEGATES: tuple[str, ...] = ()
    TOOL_NAMES: tuple[str, ...] = ()

    def functions(self) -> dict:
        return {n: getattr(self, n) for n in self.TOOL_NAMES}

    def call(self, name: str, args: dict) -> dict:
        return dispatch(self.functions(), name, args)


class TriageTools(Toolset):
    """Read-only. The triage specialist can look at the user's own data, never change anything."""

    TOOL_NAMES = ("get_account", "get_post", "search_my_tickets", "check_policy")

    def __init__(self, db: SupportDb, handle: str):
        self.db, self.handle = db, handle

    def _account(self) -> dict:
        a = self.db.get_account(self.handle)
        if a is None:
            raise LookupError(f"account {self.handle} not found")
        return a

    def get_account(self) -> dict:
        """Get the current user's account record: display name, tier, open tickets.

        Use for "who am I", "what's my tier", "what tickets do I have open".
        Read-only: changes nothing.

        Returns:
            {"handle", "display_name", "tier", "open_tickets": [{"id","issue_type","status"}]}
        """
        a = self._account()
        return {
            "handle": a["handle"],
            "display_name": a["display_name"],
            "tier": a["tier"],
            "open_tickets": [
                {"id": t["id"], "issue_type": t["issue_type"], "status": t["status"]}
                for t in self.db.open_tickets(a["id"])
            ],
        }

    def get_post(self, post_id: int) -> dict:
        """Get one post by id, including its author and visibility.

        Use when the user reports "this post ..." and gives an id, or when you found the id via search.
        Read-only: changes nothing.

        Args:
            post_id: Integer id.

        Returns:
            {"post_id", "account_id", "body", "visibility"} or {"error": "unknown_post"}.
        """
        p = self.db.get_post(post_id)
        if p is None:
            return {"error": "unknown_post", "hint": "Ask the user for the post id."}
        return {"post_id": p["id"], "account_id": p["account_id"],
                "body": p["body"], "visibility": p["visibility"]}

    def search_my_tickets(self, text: str) -> dict:
        """Find the current user's tickets whose body or issue type contains the words.

        Use for "have I reported this before", "my other ticket about X".
        Read-only: changes nothing.

        Args:
            text: A few words to match, e.g. "spam" or "login".

        Returns:
            {"tickets": [{"id","issue_type","body","status"}]}. Empty list means no match.
        """
        if not text.strip():
            return {"error": "empty_query", "hint": "Pass a few words."}
        a = self._account()
        return {"tickets": self.db.search_tickets(a["id"], text)}

    def check_policy(self) -> dict:
        """Read the platform rules that apply to the current user.

        Use BEFORE recommending that the user opens a ticket, and whenever they ask
        "am I allowed to ...". The numbers come from the policy table: never guess them.
        Read-only: changes nothing.

        Returns:
            {"max_open_tickets_per_user", "max_tickets_per_issue_type",
             "auto_escalate_after_hours", "open_now"}
        """
        a = self._account()
        return {
            "max_open_tickets_per_user": self.db.policy("max_open_tickets_per_user"),
            "max_tickets_per_issue_type": self.db.policy("max_tickets_per_issue_type"),
            "auto_escalate_after_hours": self.db.policy("auto_escalate_after_hours"),
            "open_now": len(self.db.open_tickets(a["id"])),
        }


class ResolutionTools(Toolset):
    """The resolution desk, bound to ONE handle. The model cannot pick a different user."""

    TOOL_NAMES = ("create_ticket", "assign_moderator", "escalate_ticket", "notify_user")
    SIDE_EFFECTS = ("create_ticket", "assign_moderator", "escalate_ticket", "notify_user")

    def __init__(self, db: SupportDb, handle: str, clock=lambda: datetime.now(timezone.utc)):
        self.db, self.handle, self.clock = db, handle, clock

    def _account(self) -> dict:
        a = self.db.get_account(self.handle)
        if a is None:
            raise LookupError(f"account {self.handle} not found")
        return a

    def create_ticket(self, issue_type: str, body: str) -> dict:
        """Open a support ticket for the current user. CHANGES DATA: adds a ticket.

        Use only after check_policy said it's allowed and the user has agreed. Allowed issue_type:
        abuse, spam, account, billing, other. Repeating with the same issue_type while one is open
        is safe and returns the existing ticket.

        Args:
            issue_type: one of abuse | spam | account | billing | other.
            body: 4 to 500 characters describing the problem.

        Returns:
            {"ticket_id", "status": "created" | "already_open"}, or
            {"error": "not_allowed" | "invalid_type" | "too_many"}.
        """
        a = self._account()
        if not body or len(body.strip()) < 4 or len(body) > 500:
            return {"error": "invalid_body", "hint": "4 to 500 characters."}
        # policy check FIRST so the model can't skip it
        open_now = len(self.db.open_tickets(a["id"]))
        cap = self.db.policy("max_open_tickets_per_user")
        if open_now >= cap:
            return {"error": "not_allowed", "reasons": [
                f"already has {open_now} open tickets, limit is {cap}"],
                "hint": "Tell the user to close an existing ticket first."}
        status = self.db.create_ticket(a["id"], issue_type, body)
        if status == "invalid_type":
            return {"error": "invalid_type",
                    "hint": "issue_type must be abuse, spam, account, billing or other."}
        if status == "too_many":
            return {"error": "too_many", "hint": f"Open ticket limit is {cap}."}
        if status == "already_open":
            existing = [t for t in self.db.open_tickets(a["id"]) if t["issue_type"] == issue_type]
            return {"ticket_id": existing[0]["id"] if existing else None, "status": "already_open"}
        # find the freshly created one
        newest = max(self.db.open_tickets(a["id"]), key=lambda t: t["id"])
        return {"ticket_id": newest["id"], "status": "created"}

    def assign_moderator(self, ticket_id: int, moderator: str) -> dict:
        """Assign a moderator to a ticket. CHANGES DATA: writes an assignment and moves the ticket.

        Use when the user's ticket needs a human. Do not call twice for the same ticket;
        a repeat returns the existing assignment.

        Args:
            ticket_id: integer id of the ticket.
            moderator: moderator name or handle, e.g. "@mod-lena".

        Returns:
            {"assignment_id", "ticket_id", "moderator", "status": "assigned" | "already_assigned"}.
        """
        if not moderator.strip():
            return {"error": "invalid_moderator"}
        aid, created = self.db.assign_moderator(ticket_id, moderator)
        return {"assignment_id": aid, "ticket_id": ticket_id, "moderator": moderator,
                "status": "assigned" if created else "already_assigned"}

    def escalate_ticket(self, ticket_id: int, reason: str) -> dict:
        """Escalate a ticket to platform safety. CHANGES DATA: writes an escalation.

        Use when the user reports abuse or a serious safety issue, or when policy requires it.
        A repeat returns the existing escalation.

        Args:
            ticket_id: integer id.
            reason: 5 to 200 characters.

        Returns:
            {"escalation_id", "ticket_id", "reason", "status": "escalated" | "already_escalated"}.
        """
        if not reason.strip() or len(reason) > 200:
            return {"error": "invalid_reason", "hint": "5 to 200 characters."}
        eid, created = self.db.escalate_ticket(ticket_id, reason)
        return {"escalation_id": eid, "ticket_id": ticket_id, "reason": reason,
                "status": "escalated" if created else "already_escalated"}

    def notify_user(self, message: str) -> dict:
        """Send the current user a short direct message. CHANGES DATA: a message goes out.

        Use to confirm something that just happened (ticket opened, moderator assigned). The same
        message on the same day is sent once. Never use it to answer a question; reply in chat.

        Args:
            message: 1 to 160 characters.

        Returns:
            {"notification_id", "status": "queued", "duplicate": bool}.
        """
        if not message.strip() or len(message) > 160:
            return {"error": "invalid_message", "hint": "1 to 160 characters."}
        key = notification_dedupe_key(self.handle, message, self.clock().date())
        nid, created = self.db.record_notification(self.handle, message, key)
        return {"notification_id": nid, "status": "queued", "duplicate": not created}
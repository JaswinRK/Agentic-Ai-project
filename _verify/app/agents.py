"""Three agents. A supervisor talks to the user and delegates to two specialists.

    user ──▶ supervisor ──ask_triage─────▶ triage agent      (get_account, get_post,
                                                            search_my_tickets, check_policy)
                       └─ask_resolution──▶ resolution agent  (create_ticket, assign_moderator,
                                                            escalate_ticket, notify_user)
"""
import time
from collections.abc import Callable

from app.idempotency import idempotency_key
from app.support_db import SupportDb
from app.providers import AgentError
from app.tools.support_tools import ResolutionTools, TriageTools, Toolset

SPECIALIST_MAX_STEPS = 6

SUPERVISOR_SYSTEM = """You are the Social Media Support Assistant, talking to the user {handle}.
You never look up posts or open tickets yourself. Delegate:
- ask_triage for anything read-only: the user's account, a post's details, their existing tickets, policy;
- ask_resolution for anything that changes data: open a ticket, assign a moderator, escalate, notify.
Give each specialist a complete, specific request. Then answer the user briefly, using only what
the specialists reported."""

TRIAGE_SYSTEM = """You are the triage specialist of a social media platform. Look up the user's
account, posts and existing tickets, and read the platform policy. You CANNOT change anything.
Report account id, ticket ids, issue types and the exact policy numbers. Be brief."""

RESOLUTION_SYSTEM = """You are the resolution specialist, acting for user {handle} only.
Always call check_policy-equivalent reasoning before create_ticket: the tool enforces the policy.
Use assign_moderator when a human is needed. Use escalate_ticket for abuse or safety issues.
Confirm a successful action with notify_user. Report what you did, briefly."""


def run_tool(toolset: Toolset, db: SupportDb, key: str, name: str, args: dict) -> tuple[dict, bool]:
    """Run one tool call for any agent. Returns (result, replayed)."""
    try:
        if name in toolset.DELEGATES:
            return toolset.delegate(name, args, key), False
        if name in toolset.SIDE_EFFECTS:
            result, fresh = db.once(key, name, lambda: toolset.call(name, args))
            return result, not fresh
        return toolset.call(name, args), False
    except AgentError:
        raise
    except NotImplementedError:
        return {"error": "not_implemented", "hint": f"{name} is not available yet."}, False
    except Exception as e:
        return {"error": "tool_failed",
                "hint": f"{name} failed ({type(e).__name__}). Try another way or tell the user."}, False


def run_specialist(agent: str, system: str, toolset: Toolset, *, db: SupportDb, provider,
                   task: str, parent_key: str,
                   on_step: Callable[[dict], None] | None = None) -> dict:
    """A specialist's whole agent loop, run inside one tool call of the supervisor."""
    contents = [{"role": "user", "text": task}]
    functions = list(toolset.functions().values())
    used = []
    seq = 0
    while seq < SPECIALIST_MAX_STEPS:
        turn = provider.generate(system, contents, functions)
        seq += 1
        if not turn.tool_calls:
            return {"agent": agent, "answer": turn.text or "", "tools_used": used}
        contents.append({"role": "model", "text": turn.text, "raw": turn.raw,
                         "tool_calls": [{"name": c.name, "args": c.args} for c in turn.tool_calls]})
        for call in turn.tool_calls:
            seq += 1
            key = idempotency_key(parent_key, seq, call.name, call.args)
            started = time.perf_counter()
            result, replayed = run_tool(toolset, db, key, call.name, call.args)
            used.append(call.name)
            if on_step:
                on_step({"agent": agent, "kind": "tool", "tool": call.name, "args": call.args,
                         "result": result, "ok": "error" not in result, "replayed": replayed,
                         "ms": round((time.perf_counter() - started) * 1000)})
            contents.append({"role": "tool", "name": call.name, "result": result})
    return {"agent": agent, "error": "specialist_step_limit", "tools_used": used,
            "hint": "The specialist could not finish. Ask the user to simplify."}


class SupervisorTools(Toolset):
    """The supervisor's only tools are the two specialists."""

    TOOL_NAMES = ("ask_triage", "ask_resolution")
    DELEGATES = ("ask_triage", "ask_resolution")

    def __init__(self, db: SupportDb, providers: dict, handle: str, on_step=None):
        self.db, self.providers, self.handle, self.on_step = db, providers, handle, on_step

    def ask_triage(self, question: str) -> dict:
        """Ask the triage specialist to look up the user's account, posts, existing tickets or policy.

        Use for "what's my tier", "is this post mine", "have I reported this", "am I allowed to".
        It cannot change anything.

        Args:
            question: A complete request, e.g. "Does user @priya have any open abuse tickets?"

        Returns:
            {"agent": "triage", "answer": str, "tools_used": [str]}.
        """
        raise RuntimeError("delegations run through delegate()")

    def ask_resolution(self, request: str) -> dict:
        """Ask the resolution specialist to change data for this user: open a ticket, assign a
        moderator, escalate, or send the user a message.

        Use after triage has confirmed the facts, or when the user clearly wants an action.
        Include the ticket_id or issue_type from triage. Acts only for the current user.

        Args:
            request: A complete instruction, e.g. "Open a spam ticket about post 3, then text the user."

        Returns:
            {"agent": "resolution", "answer": str, "tools_used": [str]}.
        """
        raise RuntimeError("delegations run through delegate()")

    def delegate(self, name: str, args: dict, key: str) -> dict:
        bad = self.call_check(name, args)
        if bad:
            return bad
        if self.on_step:
            self.on_step({"agent": "supervisor", "kind": "delegate", "tool": name, "args": args})
        if name == "ask_triage":
            return run_specialist("triage", TRIAGE_SYSTEM, TriageTools(self.db, self.handle),
                                  db=self.db, provider=self.providers["triage"],
                                  task=args["question"], parent_key=key, on_step=self.on_step)
        return run_specialist("resolution", RESOLUTION_SYSTEM.format(handle=self.handle),
                              ResolutionTools(self.db, self.handle),
                              db=self.db, provider=self.providers["resolution"],
                              task=args["request"], parent_key=key, on_step=self.on_step)

    def call_check(self, name: str, args: dict) -> dict | None:
        field = "question" if name == "ask_triage" else "request"
        if set(args) != {field} or not isinstance(args[field], str) or not args[field].strip():
            return {"error": "invalid_arguments", "hint": f"{name} takes one non-empty string: {field}."}
        return None
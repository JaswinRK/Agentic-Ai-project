"""Model providers. The agent only knows `generate`. (Given.)"""
from dataclasses import dataclass, field
from typing import Any


class AgentError(Exception):
    """A run could not finish. `retryable` says whether trying again later could work."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class ModelTurn:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    raw: Any = None     # provider-native content, sent back as-is (keeps Gemini's thought signatures)


# `contents` is a plain list the agent builds up:
#   {"role": "user",  "text": str}
#   {"role": "model", "text": str | None, "tool_calls": [{"name", "args"}], "raw": ...}
#   {"role": "tool",  "name": str, "result": dict}


class GeminiProvider:
    def __init__(self, model: str):
        from google import genai

        self.client = genai.Client()        # reads GEMINI_API_KEY
        self.model = model

    def _to_gemini(self, contents: list[dict]):
        from google.genai import types

        out: list = []
        for c in contents:
            if c["role"] == "user":
                out.append(types.Content(role="user", parts=[types.Part.from_text(text=c["text"])]))
            elif c["role"] == "model":
                if c.get("raw") is not None:
                    out.append(c["raw"])
                    continue
                parts = [types.Part.from_text(text=c["text"])] if c.get("text") else []
                parts += [types.Part.from_function_call(name=t["name"], args=t["args"])
                          for t in c.get("tool_calls", [])]
                out.append(types.Content(role="model", parts=parts))
            elif c["role"] == "tool":
                part = types.Part.from_function_response(name=c["name"], response=c["result"])
                # Results of parallel calls travel together in one turn.
                if out and out[-1].role == "user" and all(p.function_response for p in out[-1].parts):
                    out[-1].parts.append(part)
                else:
                    out.append(types.Content(role="user", parts=[part]))
        return out

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=tools,
            temperature=0,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            resp = self.client.models.generate_content(
                model=self.model, contents=self._to_gemini(contents), config=config)
        except errors.APIError as e:
            if e.code == 429:
                raise AgentError("provider_rate_limited", "Model quota exhausted. Wait a minute.", True) from e
            if e.code and e.code >= 500:
                raise AgentError("provider_unavailable", "Model provider failed.", True) from e
            raise AgentError("provider_error", str(e), False) from e

        content = resp.candidates[0].content if resp.candidates else None
        parts = (content.parts or []) if content else []
        text = "".join(p.text for p in parts if p.text and not p.thought) or None
        calls = [ToolCall(fc.name, dict(fc.args or {})) for fc in (resp.function_calls or [])]
        usage = resp.usage_metadata
        return ModelTurn(text=text, tool_calls=calls,
                         tokens_in=(usage.prompt_token_count or 0) if usage else 0,
                         tokens_out=(usage.candidates_token_count or 0) if usage else 0,
                         raw=content)


class ScriptedProvider:
    """Replays a fixed list of turns in call order. No network, no quota. Used by the tests."""

    model = "mock"

    def __init__(self, script: list, loop: bool = False):
        self.original, self.script, self.loop = list(script), list(script), loop
        self.calls: list[list[dict]] = []      # what the agent sent on each call

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        self.calls.append([dict(c) for c in contents])
        if not self.script and self.loop:
            self.script = list(self.original)
        if not self.script:
            return ModelTurn(text="(mock) script exhausted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class PositionalMock:
    """A scripted model that answers by position in the current turn, not by call count.
    A fresh process that resumes a half-finished run gets the NEXT turn, not the first one.
    `slow` sleeps before each answer, so you have time to kill the worker mid-run."""

    model = "mock"

    def __init__(self, turns: list[ModelTurn], slow: float = 0.0):
        self.turns, self.slow = turns, slow
        self.calls: list[list[dict]] = []

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        import time

        self.calls.append([dict(c) for c in contents])
        last_user = max(i for i, c in enumerate(contents) if c["role"] == "user")
        position = sum(1 for c in contents[last_user:] if c["role"] == "model")
        if self.slow:
            time.sleep(self.slow)
        if position >= len(self.turns):
            return ModelTurn(text="(mock) nothing more to do.")
        return self.turns[position]


class RoutedMock:
    """Several scripted conversations in one mock: picks a script by a phrase in the current request,
    then answers by position (like PositionalMock). Used by the demo and the tests."""

    model = "mock"

    def __init__(self, routes: dict[str, list[ModelTurn]], slow: float = 0.0):
        self.routes, self.slow = routes, slow
        self.calls: list[list[dict]] = []

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        import time

        self.calls.append([dict(c) for c in contents])
        last_user = max(i for i, c in enumerate(contents) if c["role"] == "user")
        request = contents[last_user]["text"]
        position = sum(1 for c in contents[last_user:] if c["role"] == "model")
        if self.slow:
            time.sleep(self.slow)
        for phrase, turns in self.routes.items():
            if phrase.lower() in request.lower():
                return turns[position] if position < len(turns) else ModelTurn(text="(mock) done.")
        return ModelTurn(text="(mock) I have no script for that request.")


def _call(name, **args):
    return ModelTurn(text=None, tool_calls=[ToolCall(name, args)], tokens_in=100, tokens_out=10)


def demo_providers(slow: float = 0.0) -> dict:
    """Scripted models for the three agents, covering the two demo questions."""
    return {
        "supervisor": RoutedMock({
            "spam post": [
                _call("ask_triage", question="Does @priya have any open tickets, and what is post 3?"),
                _call("ask_resolution", request="Open a spam ticket about post 3 for @priya and text her to confirm."),
                ModelTurn(text="(mock) Done: I've opened a spam ticket and sent you a confirmation DM."),
            ],
            "abuse": [
                _call("ask_triage", question="What is my open ticket list, and what's the policy on abuse tickets?"),
                _call("ask_resolution", request="Open an abuse ticket for @arjun and escalate it immediately."),
                ModelTurn(text="(mock) I've opened an abuse ticket and escalated it to platform safety."),
            ],
        }, slow),
        "triage": RoutedMock({
            "post 3": [
                _call("get_account"),
                _call("get_post", post_id=3),
                ModelTurn(text="(mock) @priya has 0 open tickets. Post 3 is by @arjun, visibility public, "
                               "body: 'Anyone else's Wi-Fi dropping every 10 minutes?'."),
            ],
            "abuse tickets": [
                _call("get_account"),
                _call("check_policy"),
                ModelTurn(text="(mock) @arjun has 0 open tickets. Policy: max 3 open tickets, 1 per issue type."),
            ],
        }, slow),
        "resolution": RoutedMock({
            "post 3": [
                _call("create_ticket", issue_type="spam",
                      body="Post 3 appears to be spam about Wi-Fi problems."),
                _call("notify_user",
                      message="Your spam report about post 3 has been filed. We'll be in touch."),
                ModelTurn(text="(mock) Opened a spam ticket and sent the confirmation DM."),
            ],
            "abuse ticket": [
                _call("create_ticket", issue_type="abuse",
                      body="User reports harassment in a DM thread."),
                _call("escalate_ticket", ticket_id=1,
                      reason="Abuse reports escalate to platform safety immediately."),
                ModelTurn(text="(mock) Opened an abuse ticket and escalated it to safety."),
            ],
        }, slow),
    }
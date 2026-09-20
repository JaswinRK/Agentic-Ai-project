from app.agents import SupervisorTools, run_specialist, run_tool
from app.providers import ModelTurn, ScriptedProvider, ToolCall, demo_providers
from app.tools.support_tools import TriageTools


def test_the_supervisor_only_has_delegation_tools(db):
    tools = SupervisorTools(db, demo_providers(), "@priya")
    assert set(tools.functions()) == {"ask_triage", "ask_resolution"}
    assert set(tools.DELEGATES) == set(tools.TOOL_NAMES)


def test_triage_specialist_answers_a_question(db):
    result, replayed = run_tool(
        SupervisorTools(db, demo_providers(), "@priya"), db, "k1", "ask_triage",
        {"question": "What is post 3?"})
    assert result["agent"] == "triage" and not replayed


def test_resolution_specialist_opens_and_notifies(db):
    result, _ = run_tool(
        SupervisorTools(db, demo_providers(), "@priya"), db, "k1", "ask_resolution",
        {"request": "Open a spam ticket about post 3 for @priya and text her to confirm."})
    assert "create_ticket" in result["tools_used"]
    assert "notify_user" in result["tools_used"]


def test_a_repeated_delegation_with_the_same_key_does_nothing_twice(db):
    tools = SupervisorTools(db, demo_providers(), "@priya")
    args = {"request": "Open a spam ticket about post 3 for @priya and text her to confirm."}
    run_tool(tools, db, "same-key", "ask_resolution", args)
    run_tool(tools, db, "same-key", "ask_resolution", args)
    # one create + one notify went through the idempotency table
    assert db.count("idempotency") >= 2


def test_bad_delegation_arguments_are_fed_back(db):
    result, _ = run_tool(SupervisorTools(db, demo_providers(), "@priya"), db, "k",
                         "ask_resolution", {"request": ""})
    assert result["error"] == "invalid_arguments"


def test_a_looping_specialist_stops(db):
    looping = ScriptedProvider(
        [ModelTurn(text=None, tool_calls=[ToolCall("get_post", {"post_id": 3})])], loop=True)
    result = run_specialist("triage", "sys", TriageTools(db, "@priya"),
                            db=db, provider=looping, task="x", parent_key="k")
    assert result["error"] == "specialist_step_limit"


def test_specialists_see_only_their_own_task(db):
    providers = demo_providers()
    run_tool(SupervisorTools(db, providers, "@priya"), db, "k",
             "ask_triage", {"question": "What is post 3?"})
    seen = providers["triage"].calls[0]
    assert seen == [{"role": "user", "text": "What is post 3?"}]
    assert providers["resolution"].calls == []
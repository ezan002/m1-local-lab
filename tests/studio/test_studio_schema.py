import copy

import pytest
from pydantic import ValidationError

from m1_local.studio.runtime_support import TemplateError, ToolError, render, tool_calculator, tool_http_get
from m1_local.studio.schema import WorkflowDefinition
from m1_local.studio.seeds import calculator_router, research_review


def test_seed_workflows_are_valid():
    WorkflowDefinition.model_validate(research_review())
    WorkflowDefinition.model_validate(calculator_router())


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["nodes"].pop(0), "exactly one start node"),
    (lambda d: d["edges"].pop(), "needs exactly one 'true' and one 'false' edge"),
    (lambda d: d["edges"].append({"source": "calc", "target": "ghost"}), "unknown node"),
    (lambda d: d["nodes"][1]["config"].update(tool=None), "needs a tool"),
    (lambda d: d["edges"].append({"source": "large", "target": "start"}), "cannot have"),
])
def test_invalid_structures_are_rejected(mutate, message):
    definition = calculator_router()
    mutate(definition)
    with pytest.raises(ValidationError) as info:
        WorkflowDefinition.model_validate(definition)
    assert message in str(info.value)


def test_cycle_must_pass_through_condition():
    definition = research_review()
    # planner <-> researcher loop without a condition can never terminate
    definition["edges"] = [e for e in definition["edges"] if e["id"] != "e3"] + [{"source": "researcher", "target": "planner"}]
    with pytest.raises(ValidationError) as info:
        WorkflowDefinition.model_validate(definition)
    assert "cycles must pass through a condition" in str(info.value) or "unreachable" in str(info.value)


def test_agent_requires_prompt_and_node_ids_are_validated():
    definition = research_review()
    bad = copy.deepcopy(definition)
    bad["nodes"][1]["config"]["prompt"] = "  "
    with pytest.raises(ValidationError, match="needs a prompt"):
        WorkflowDefinition.model_validate(bad)
    bad = copy.deepcopy(definition)
    bad["nodes"][1]["id"] = "Bad Id"
    with pytest.raises(ValidationError, match="node id must match"):
        WorkflowDefinition.model_validate(bad)


def test_templates_resolve_paths_and_fail_loudly():
    state = {"input": {"topic": "x"}, "nodes": {"calc": {"output": {"result": 42}}}}
    assert render("{{input.topic}} = {{nodes.calc.output.result}}", state) == "x = 42"
    assert render("[{{nodes.missing.output?}}]", state) == "[]"
    with pytest.raises(TemplateError, match="nodes.missing.output"):
        render("{{nodes.missing.output}}", state)


def test_calculator_is_safe_and_reports_errors():
    assert tool_calculator("12 * (7 + 5)")["result"] == 144
    for expression in ("__import__('os')", "1/0", "2**1000", "open('x')"):
        with pytest.raises(ToolError):
            tool_calculator(expression)


def test_http_get_refuses_private_addresses():
    with pytest.raises(ToolError, match="private/loopback"):
        tool_http_get("http://127.0.0.1:1/")
    with pytest.raises(ToolError, match="absolute http"):
        tool_http_get("file:///etc/passwd")

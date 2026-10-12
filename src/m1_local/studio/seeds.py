"""Reference workflows created on first start (only when the workflow table is empty)."""
from __future__ import annotations

from typing import Any

from .store import StudioStore

RESEARCH_ID = "wf_research_review"
CALC_ID = "wf_calculator_router"


def research_review() -> dict[str, Any]:
    return {
        "default_provider": "openai",
        "default_model": "",
        "nodes": [
            {"id": "start", "type": "start", "label": "Brief", "position": {"x": 0, "y": 160},
             "config": {"inputs": [{"name": "topic", "label": "Topic", "default": "Why do multi-agent LLM systems fail?"},
                                   {"name": "audience", "label": "Audience", "default": "graduate students"}]}},
            {"id": "planner", "type": "agent", "label": "Planner", "position": {"x": 260, "y": 160},
             "config": {"role": "planner",
                        "system_prompt": "You are a planning agent. Produce a short numbered research plan (max 4 steps).",
                        "prompt": "Topic: {{input.topic}}\nAudience: {{input.audience}}\nWrite the plan.",
                        "mock_response": "Plan for '{{input.topic}}':\n1. Identify common failure classes\n2. Count the plan's words for scope\n3. Draft findings for {{input.audience}}"}},
            {"id": "researcher", "type": "agent", "label": "Researcher", "position": {"x": 520, "y": 160},
             "config": {"role": "researcher",
                        "system_prompt": "You are a research agent. Use tools when they help. Be concise (<150 words).",
                        "prompt": "Follow this plan and write findings.\nPlan:\n{{nodes.planner.output}}\n\nReviewer feedback so far (may be empty): {{nodes.reviewer.output?}}",
                        "tools": ["word_count", "calculator"], "max_tool_rounds": 2,
                        "mock_tool_calls": [{"name": "word_count", "arguments": {"text": "{{nodes.planner.output}}"}}],
                        "mock_response": "Findings on '{{input.topic}}': failures cluster into specification, coordination and verification errors. Tool evidence: {{tool_results}}"}},
            {"id": "reviewer", "type": "agent", "label": "Reviewer", "position": {"x": 780, "y": 160},
             "config": {"role": "reviewer",
                        "system_prompt": "You are a strict reviewer. Reply with exactly 'APPROVED' or 'REVISE: <one concrete fix>'.",
                        "prompt": "Findings to review:\n{{nodes.researcher.output}}",
                        "mock_response": "REVISE: add one quantitative example."}},
            {"id": "needs_revision", "type": "condition", "label": "Needs revision?", "position": {"x": 1040, "y": 160},
             "config": {"left": "{{nodes.reviewer.output}}", "op": "contains", "right": "REVISE", "max_true_visits": 2}},
            {"id": "final", "type": "output", "label": "Final brief", "position": {"x": 1300, "y": 160},
             "config": {"template": "# Brief: {{input.topic}}\n\n{{nodes.researcher.output}}\n\nReview: {{nodes.reviewer.output}}"}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "planner"},
            {"id": "e2", "source": "planner", "target": "researcher"},
            {"id": "e3", "source": "researcher", "target": "reviewer"},
            {"id": "e4", "source": "reviewer", "target": "needs_revision"},
            {"id": "e5", "source": "needs_revision", "target": "researcher", "branch": "true"},
            {"id": "e6", "source": "needs_revision", "target": "final", "branch": "false"},
        ],
    }


def calculator_router() -> dict[str, Any]:
    return {
        "default_provider": "deterministic",
        "nodes": [
            {"id": "start", "type": "start", "label": "Expression", "position": {"x": 0, "y": 120},
             "config": {"inputs": [{"name": "expression", "label": "Arithmetic expression", "default": "12 * (7 + 5)"}]}},
            {"id": "calc", "type": "tool", "label": "Calculator", "position": {"x": 260, "y": 120},
             "config": {"tool": "calculator", "arguments": {"expression": "{{input.expression}}"}}},
            {"id": "is_large", "type": "condition", "label": "Result > 100?", "position": {"x": 520, "y": 120},
             "config": {"left": "{{nodes.calc.output.result}}", "op": "greater_than", "right": "100"}},
            {"id": "large", "type": "output", "label": "Large result", "position": {"x": 780, "y": 40},
             "config": {"template": "LARGE: {{input.expression}} = {{nodes.calc.output.result}}"}},
            {"id": "small", "type": "output", "label": "Small result", "position": {"x": 780, "y": 200},
             "config": {"template": "small: {{input.expression}} = {{nodes.calc.output.result}}"}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "calc"},
            {"id": "e2", "source": "calc", "target": "is_large"},
            {"id": "e3", "source": "is_large", "target": "large", "branch": "true"},
            {"id": "e4", "source": "is_large", "target": "small", "branch": "false"},
        ],
    }


def seed(store: StudioStore) -> list[str]:
    if store.list_workflows():
        return []
    store.save_workflow(RESEARCH_ID, "Research → Review loop",
                        "Planner, tool-using researcher and reviewer agents with a revision loop. Real model or deterministic script.",
                        research_review())
    store.save_workflow(CALC_ID, "Calculator router (no LLM)",
                        "Tool node + condition routing; runs fully offline.", calculator_router())
    return [RESEARCH_ID, CALC_ID]

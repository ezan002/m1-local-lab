"""An independently written LangGraph agent, made debuggable by Agent Studio with the studio-executor SDK.

It does not import anything from Agent Studio or m1_local: it is an ordinary LangGraph program with its own
SQLite checkpointer, plus ~10 lines that expose it (see the bottom of the file).

Graph:  classify -> lookup (tool: knowledge base) -> draft -> review --APPROVED--> finalize
                                                        ^-----REVISE (max 2)-----'

    python examples/langgraph_agent/support_agent.py --port 8020                   # deterministic, offline
    python examples/langgraph_agent/support_agent.py --port 8020 --mode openai     # real model (OPENAI_API_KEY)

Then in Agent Studio: Connected agents -> Connect executor -> "Any LangGraph agent (studio-executor SDK)",
URL http://127.0.0.1:8020 and the token printed at startup (or set STUDIO_EXECUTOR_TOKEN beforehand).
"""
from __future__ import annotations

import argparse
import asyncio
import operator
import os
import sys
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

KNOWLEDGE_BASE = {
    "billing": ["Refunds are issued to the original payment method within 5 business days.",
                "Invoices can be downloaded from Settings > Billing.",
                "Plan changes are prorated to the day."],
    "technical": ["Clear the browser cache and sign in again if the dashboard does not load.",
                  "Status updates are posted at status.example.com.",
                  "Export jobs larger than 2 GB are split into several files."],
    "general": ["Support is available on weekdays from 9:00 to 18:00 CET.",
                "Account owners can add teammates from Settings > Members."],
}


class SupportState(TypedDict, total=False):
    question: str
    category: str            # replaced by classify; editable in a fork
    facts: list[str]         # replaced by lookup; editable in a fork
    draft: str               # replaced by draft; editable in a fork
    review: str              # "APPROVED" or "REVISE: <reason>"; editable in a fork
    revisions: int
    answer: str
    trail: Annotated[list[str], operator.add]   # append-only log (a reducer: not editable)


class Model:
    """Deterministic script or an OpenAI-compatible chat model."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        if mode == "openai":
            from openai import AsyncOpenAI

            if not os.environ.get("OPENAI_API_KEY"):
                raise SystemExit("OPENAI_API_KEY is not set; use --mode deterministic")
            self.client = AsyncOpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None)
            self.model = os.environ.get("SUPPORT_AGENT_MODEL", "gpt-4.1-mini")

    async def chat(self, system: str, user: str) -> str:
        response = await self.client.chat.completions.create(
            model=self.model, temperature=0.2, max_tokens=500,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return (response.choices[0].message.content or "").strip()

    async def classify(self, question: str) -> str:
        if self.mode == "openai":
            label = (await self.chat("Classify the support question. Reply with exactly one word: billing, technical or general.",
                                     question)).lower().strip(" .")
            return label if label in KNOWLEDGE_BASE else "general"
        q = question.lower()
        return "billing" if any(w in q for w in ("refund", "invoice", "charge", "plan")) else \
            "technical" if any(w in q for w in ("error", "load", "export", "bug", "crash")) else "general"

    async def draft(self, question: str, facts: list[str], feedback: str) -> str:
        if self.mode == "openai":
            return await self.chat("Write a short, friendly support answer using only the given facts.",
                                   f"Question: {question}\nFacts: {facts}\nReviewer feedback to address: {feedback or 'none'}")
        used = facts if feedback else facts[:1]
        return f"Thanks for reaching out. {' '.join(used)}"

    async def review(self, question: str, facts: list[str], draft: str) -> str:
        if self.mode == "openai":
            return await self.chat("You review support answers. Reply 'APPROVED' if the draft answers the question using the "
                                   "facts, otherwise 'REVISE: <what is missing>'.",
                                   f"Question: {question}\nFacts: {facts}\nDraft: {draft}")
        missing = [f for f in facts if f not in draft]
        return "APPROVED" if not missing else f"REVISE: also mention: {missing[0]}"


def build(model: Model, checkpointer):
    async def classify(state: SupportState) -> dict:
        category = await model.classify(state["question"])
        return {"category": category, "revisions": 0, "trail": [f"classify -> {category}"]}

    async def lookup(state: SupportState) -> dict:
        facts = KNOWLEDGE_BASE.get(state.get("category", ""), KNOWLEDGE_BASE["general"])
        return {"facts": list(facts), "trail": [f"lookup {len(facts)} facts"]}

    async def draft(state: SupportState) -> dict:
        feedback = state.get("review", "") if str(state.get("review", "")).startswith("REVISE") else ""
        text = await model.draft(state["question"], state.get("facts", []), feedback)
        return {"draft": text, "revisions": state.get("revisions", 0) + (1 if feedback else 0), "trail": ["draft"]}

    async def review(state: SupportState) -> dict:
        verdict = await model.review(state["question"], state.get("facts", []), state.get("draft", ""))
        return {"review": verdict, "trail": [f"review -> {verdict[:40]}"]}

    async def finalize(state: SupportState) -> dict:
        return {"answer": state.get("draft", ""), "trail": ["finalize"]}

    def after_review(state: SupportState) -> str:
        approved = str(state.get("review", "")).strip().upper().startswith("APPROVED")
        return "finalize" if approved or state.get("revisions", 0) >= 2 else "draft"

    builder = StateGraph(SupportState)
    for name, fn in {"classify": classify, "lookup": lookup, "draft": draft, "review": review, "finalize": finalize}.items():
        builder.add_node(name, fn)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "lookup")
    builder.add_edge("lookup", "draft")
    builder.add_edge("draft", "review")
    builder.add_conditional_edges("review", after_review, ["draft", "finalize"])
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--mode", choices=["deterministic", "openai"], default="deterministic")
    parser.add_argument("--data-dir", type=Path, default=Path("data/examples/support-agent"))
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    saver = AsyncSqliteSaver(aiosqlite.connect(str(args.data_dir / "checkpoints.sqlite")))
    await saver.setup()
    graph = build(Model(args.mode), saver)

    # ---- the only Studio-specific part: describe what may be started and edited, then serve -------------
    from studio_executor import ForkField, StudioExecutor, serve_async

    executor = StudioExecutor(
        graph, name="Support answer agent", provider_mode="openai" if args.mode == "openai" else "deterministic",
        input_fields=[{"name": "question", "label": "Customer question",
                       "default": "I was charged twice, how do I get a refund?"}],
        make_input=lambda inputs: {"question": inputs.get("question", "").strip() or "Hello", "trail": []},
        output=lambda values: {"answer": values.get("answer"), "category": values.get("category"),
                               "revisions": values.get("revisions")},
        fork_fields=[ForkField("category", "string", "Classifier result (billing / technical / general). Edit after classify."),
                     ForkField("facts", "string_list", "Knowledge-base facts found by lookup. Edit after lookup."),
                     ForkField("draft", "string", "Answer draft. Edit after draft to see how the reviewer reacts."),
                     ForkField("review", "string", "Reviewer verdict: APPROVED or REVISE: <reason>. Edit after review.")],
        node_types={"classify": "agent", "lookup": "tool", "draft": "agent", "review": "agent", "finalize": "node"},
        registry_path=args.data_dir / "runs.sqlite",
        notes=["Example agent from examples/langgraph_agent; independent of Agent Studio's code."],
    )
    await serve_async(executor, port=args.port)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Ctrl+C: uvicorn has already shut down cleanly
        print("[studio-executor] stopped")

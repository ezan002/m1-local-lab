"""Expose langchain-ai/local-deep-researcher (a third-party LangGraph agent) to Agent Studio.

Nothing in the local-deep-researcher repository is changed. This script imports its *uncompiled* graph builder,
compiles it with a SQLite checkpointer, describes what Studio may start and edit, and serves it with the
studio_executor SDK. Run it with the researcher's own virtual environment:

    C:\\projects\\local-deep-researcher\\.venv\\Scripts\\python.exe ^
        C:\\projects\\m1-local-lab\\examples\\third_party\\local_deep_researcher_studio.py ^
        --repo C:\\projects\\local-deep-researcher --port 8030

Then: Agent Studio -> Connected agents -> Connect executor -> "Any LangGraph agent", URL http://127.0.0.1:8030 and
the printed token. Requires Ollama running with the model named in the researcher's .env (LOCAL_LLM).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
STUDIO_SRC = HERE.parents[2] / "src"          # m1-local-lab/src: only the studio_executor package is used
DEFAULT_DATA = HERE.parents[2] / "data" / "third_party" / "local-deep-researcher"


def load_env(repo: Path) -> None:
    """The researcher reads its settings (LLM_PROVIDER, LOCAL_LLM, SEARCH_API, ...) from environment variables;
    `langgraph dev` normally loads its .env. Load it the same way, without overriding variables already set."""
    env_file = repo / ".env"
    if env_file.is_file():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)


async def build_executor(data_dir: Path, registry: bool = True):
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from ollama_deep_researcher.graph import builder   # the third party's own graph definition

    from studio_executor import ForkField, StudioExecutor

    data_dir.mkdir(parents=True, exist_ok=True)
    saver = AsyncSqliteSaver(aiosqlite.connect(str(data_dir / "checkpoints.sqlite")))
    await saver.setup()
    graph = builder.compile(checkpointer=saver)        # same builder, plus a checkpointer
    model = os.environ.get("LOCAL_LLM", "llama3.2")
    loops = os.environ.get("MAX_WEB_RESEARCH_LOOPS", "3")
    executor = StudioExecutor(
        graph,
        name=f"local-deep-researcher ({model})",
        provider_mode="unknown",  # a real local model, not OpenAI and not scripted
        input_fields=[{"name": "research_topic", "label": "Research topic",
                       "default": "How do LangGraph checkpoints enable time travel debugging?"}],
        make_input=lambda inputs: {"research_topic": inputs.get("research_topic", "").strip()},
        output=lambda v: {"running_summary": v.get("running_summary"), "research_loop_count": v.get("research_loop_count"),
                          "last_search_query": v.get("search_query"), "sources": len(v.get("sources_gathered") or [])},
        fork_fields=[
            ForkField("search_query", "string",
                      "Next web search. Edit at a checkpoint after generate_query or reflect_on_summary (next: web_research)."),
            ForkField("running_summary", "string",
                      "Summary so far. Edit after summarize_sources to see how reflection and later loops react."),
            ForkField("research_loop_count", "number",
                      f"Loops done; after reflect_on_summary the agent loops while it is <= MAX_WEB_RESEARCH_LOOPS ({loops}). "
                      "Raise it at a checkpoint after summarize_sources (next: reflect_on_summary) to stop after this round."),
        ],
        node_types={"generate_query": "agent", "web_research": "tool", "summarize_sources": "agent",
                    "reflect_on_summary": "agent", "finalize_summary": "node"},
        registry_path=(data_dir / "runs.sqlite") if registry else None,
        notes=[f"Third-party code: langchain-ai/local-deep-researcher, unmodified. Model {model} via "
               f"{os.environ.get('LLM_PROVIDER', 'ollama')}, search {os.environ.get('SEARCH_API', 'duckduckgo')}, "
               f"MAX_WEB_RESEARCH_LOOPS={loops}.",
               "Nodes are synchronous in this project; LangGraph runs them in worker threads, so a pause waits for the "
               "running step (a model call or a web search) to finish."],
    )
    executor.saver_connection = saver.conn  # closed by main() on shutdown
    return executor


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(r"C:\projects\local-deep-researcher"),
                        help="local-deep-researcher checkout (for its .env)")
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
    load_env(args.repo)
    from studio_executor import serve_async

    executor = await build_executor(args.data_dir)
    try:
        await serve_async(executor, port=args.port)
    finally:
        await executor.saver_connection.close()


if __name__ == "__main__":
    sys.path.insert(0, str(STUDIO_SRC))
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Ctrl+C: uvicorn has already shut down cleanly
        print("[studio-executor] stopped")

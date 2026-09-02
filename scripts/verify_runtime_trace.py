from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from phoenix.client import Client

from m1_local.config import PROJECT_ROOT, Settings
from m1_local.models import RuleBasedGateway
from m1_local.runtime import AgentRuntime


async def verify() -> int:
    """Export only deterministic offline spans; this script never calls a real model."""
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = Settings(
            checkpoint_db=root / "checkpoints.sqlite",
            metadata_db=root / "app.sqlite",
            artifact_root=root / "artifacts",
            phoenix_working_dir=PROJECT_ROOT / "data/phoenix",
            allowed_file_dirs=[str(PROJECT_ROOT / "datasets")],
            headless=True,
        )
        runtime = AgentRuntime(settings, RuleBasedGateway(), enable_tracing=True)
        await runtime.startup()
        tracing_warning = runtime.observability.warning
        provider_type = type(runtime.observability.provider).__name__ if runtime.observability.provider else None
        secret = "sk-offline-verification-secret"
        try:
            original = await runtime.create_run(
                f"Summarize the explicitly allowed sample CSV; api_key={secret}",
                allowed_files=[str(PROJECT_ROOT / "datasets/sample.csv")],
                dataset_case_id="offline-trace-verification",
            )
            source = next(item for item in await runtime.history(original) if item["next"])
            replayed = await runtime.replay(original, source["checkpoint_id"])
            forked = await runtime.fork(
                original, source["checkpoint_id"], {"worker_instruction": "continue safely"}
            )
            await runtime.resume(forked)
            links = []
            with runtime.store._connect() as db:  # acceptance-only local linkage inspection
                links = [dict(row) for row in db.execute("SELECT * FROM checkpoint_links")]
        finally:
            await runtime.shutdown()

        run_ids = {original, replayed, forked}
        client = Client(base_url=settings.phoenix_base_url)
        frame = client.spans.get_spans_dataframe(
            project_identifier=settings.phoenix_project_name, limit=2000, timeout=10
        )
        run_column = "attributes.run_id"
        selected = (
            frame[frame[run_column].astype(str).isin(run_ids)]
            if run_column in frame.columns else frame.iloc[0:0]
        )
        app_spans = selected[selected["name"].astype(str).str.startswith("m1.")] if not selected.empty else selected
        names = set(app_spans["name"].astype(str)) if not app_spans.empty else set()
        kinds = set(app_spans["attributes.openinference.span.kind"].dropna().astype(str)) if not app_spans.empty else set()
        serialized = app_spans.to_json(force_ascii=False, date_format="iso") if not app_spans.empty else ""

        required_columns = {
            "attributes.run_id", "attributes.thread_id", "attributes.branch_id",
            "attributes.status", "attributes.input.value", "attributes.output.value",
        }
        node_columns = {
            "attributes.execution_id", "attributes.langgraph_node",
            "attributes.agent_name", "attributes.step_number",
        }
        commits = app_spans[app_spans["name"].astype(str) == "m1.checkpoint.commit"] if not app_spans.empty else app_spans
        nodes = app_spans[app_spans["name"].astype(str).str.startswith("m1.node.")] if not app_spans.empty else app_spans
        replay_rows = app_spans[app_spans.get("attributes.replay_of", "").astype(str) == original] if "attributes.replay_of" in app_spans else app_spans.iloc[0:0]
        fork_rows = app_spans[app_spans.get("attributes.fork_of", "").astype(str) == original] if "attributes.fork_of" in app_spans else app_spans.iloc[0:0]

        passed = (
            {"m1.run", "m1.node.planner", "m1.node.orchestrator", "m1.node.pause_gate", "m1.node.file_reader",
             "m1.node.finalizer", "m1.checkpoint.commit"}.issubset(names)
            and {"AGENT", "CHAIN", "LLM", "TOOL"}.issubset(kinds)
            and required_columns.issubset(app_spans.columns)
            and node_columns.issubset(nodes.columns)
            and app_spans[list(required_columns)].notna().all().all()
            and nodes[list(node_columns)].notna().all().all()
            and not commits.empty
            and commits["attributes.checkpoint_id"].notna().all()
            and nodes["parent_id"].notna().all()
            and not replay_rows.empty
            and not fork_rows.empty
            and secret not in serialized
            and links
            and all(link["output_checkpoint_id"] and link["status"] == "committed" for link in links)
        )
        print(json.dumps({
            "status": "PASS" if passed else "FAIL",
            "run_ids": {"original": original, "replay": replayed, "fork": forked},
            "trace_ids": sorted(set(selected["context.trace_id"].astype(str))) if not selected.empty else [],
            "span_names": sorted(names),
            "openinference_kinds": sorted(kinds),
            "checkpoint_links": len(links),
            "link_trace_ids": sorted({str(link["trace_id"]) for link in links}),
            "tracing_warning": tracing_warning,
            "provider_type": provider_type,
            "application_spans": len(app_spans),
            "raw_secret_absent": secret not in serialized,
        }, ensure_ascii=False))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))

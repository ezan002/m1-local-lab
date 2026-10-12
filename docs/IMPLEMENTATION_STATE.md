# Implementation State — Agent Studio & Observability Platform V1

Persistent journal. On resume: read this file, `git log --oneline -15`, then continue from "Next actions".
Branch: `feature/agent-studio-v1` (baseline commit `f933499` = previous observation work).

## Architecture decisions (2026-10-12)

- **New product lives in `src/m1_local/studio/`**, served by `m1-local studio` (FastAPI, port 8100 by default).
  Legacy apps (`observe`, `serve`, attribution) are untouched and still runnable.
- **Own normalized execution store** (`data/studio/studio.sqlite`): workflows, runs (with parent/branch lineage,
  definition snapshot, capabilities), spans (parent-child, kind, input/output/error, timings, checkpoint ref).
  UI never depends on Phoenix; Phoenix remains an optional legacy viewer.
- **Runtime: LangGraph 1.2.11 + AsyncSqliteSaver** (`data/studio/checkpoints.sqlite`). A workflow definition
  (nodes: start / agent / tool / condition / output) is compiled into a `StateGraph`.
  - Pause: per-run flag checked at node start → `interrupt()`; breakpoints use the same path.
  - Resume: same thread, `Command(resume=...)`/`None`.
  - Fork / replay-from-checkpoint: copy the checkpoint's values into a **new thread** via
    `aupdate_state(..., as_node=<node that produced it>)`, apply whitelisted edits
    (`input.*`, `nodes.<id>.output`), run → original thread is never touched.
  - Retry of failed node: resume failed run from its last checkpoint.
- **LLM providers**: `openai` (OpenAI-compatible, key from env only, never stored) and `deterministic`
  (scripted responses, clearly labeled; used for automated tests and replay semantics).
- **External integration**: OTLP/HTTP ingest at `/v1/traces` (protobuf + JSON) → read-only external runs.
  Executable integration: `examples/external_agent/research_agent.py`, launched by the app as a subprocess,
  instruments itself with the OpenTelemetry SDK and exports to the ingest endpoint (trace-only capabilities).
- **Frontend**: React + Vite + @xyflow/react in `frontend/`, built to `web/studio/` (committed build so the
  app starts without Node).

## Status

### Milestone 1 — backend core (DONE, verified by real execution 2026-10-12)
Files: `src/m1_local/studio/{schema,store,runtime_support,engine,seeds,otlp,integrations,app}.py`,
`examples/external_agent/research_agent.py`, `m1-local studio` CLI command.
Verified (manual smoke, real server on 8100 with `data/studio-dev`):
- deterministic research workflow: completes, revision loop guard, 14 checkpoints; fork from reviewer
  checkpoint with `nodes.reviewer.output=APPROVED` takes a different path; source thread unchanged.
- real OpenAI run (`gpt-4.1-mini-2025-04-14`) of research workflow: completed, 3 llm spans with model/usage.
- calculator `1/0` -> run failed, ToolError on node `calc`.
- external agent launched via API: deterministic ok / `--fail-tool` error captured (exception + stack) /
  real OpenAI ok; external runs reject resume (409) and report checkpoints unsupported.

## Next actions

1. Frontend (React + Vite + @xyflow/react) in `frontend/`, build to `web/studio/`.
2. Automated tests `tests/studio/` (engine, API, OTLP, restart recovery) using temp dirs.
3. Browser validation + docs (V1_VALIDATION_REPORT.md, V1_DELIVERY_REPORT.md).

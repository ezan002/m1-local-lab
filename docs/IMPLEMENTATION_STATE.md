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

### Milestone 2 — frontend (DONE, browser-verified)
`frontend/` (React 18 + @xyflow/react 12 + Vite 6) built to `web/studio/` (committed). Pages: Workflows,
Editor (palette, canvas, inspector, validate, save, run dialog w/ provider + breakpoints, live overlay),
Runs, Run detail (graph, timeline, spans drill-down, checkpoints + fork/replay, compare, I/O), External traces,
Integrations (launch agent, OTLP instructions), Settings. Bugs found+fixed in browser: fitView timing, cached
index.html, stale CSRF after server restart, visit counting, inherited nodes in forks/compare.

### Milestone 3 — tests and evidence (DONE)
- `tests/studio/` 30 tests (schema, engine on real LangGraph+SQLite, API+OTLP protobuf from real OTel SDK,
  real server subprocess e2e incl. restart). Full suite: 199 passed / 3 skipped (before start-defaults test).
- `scripts/verify_studio_ui.py` -> `docs/evidence/v1/ui/` PASS, 0 console/page/network errors.
- `scripts/validate_v1_real_model.py` -> `docs/evidence/v1/real-model/receipt.json` PASS (5 checks, gpt-4.1-mini).
  Found+fixed: start defaults not propagated into `input.*` (real bug).

## Next actions

1. `scripts/start-studio.ps1`, README section, delete-workflow button.
2. `docs/V1_VALIDATION_REPORT.md`, `docs/V1_DELIVERY_REPORT.md` (completion matrix).
3. Final regression (full pytest + UI script), launch daily Studio on 8100 with `data/studio`.

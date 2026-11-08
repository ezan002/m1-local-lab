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

### Milestone 4 — delivery (DONE 2026-10-12)
- `scripts/start-studio.ps1`, README section, delete-workflow button, cancel closes in-flight spans.
- Final: full pytest 202 passed / 3 skipped; `tests/studio` 32 passed; UI script PASS (0 browser errors);
  real-model script PASS 5/5; real-model mid-run pause/resume verified on daily server.
- Reports: `docs/V1_VALIDATION_REPORT.md`, `docs/V1_DELIVERY_REPORT.md` (completion matrix).
- Daily Studio left running: http://127.0.0.1:8100, data `data/studio`, started via
  `scripts/restart_studio_dev.ps1 -Port 8100 -DataDir data/studio` (stop: same script stops only an m1_local studio process,
  or Ctrl+C when started with `start-studio.ps1`).

### Milestone 5 — connected executors, route 3 (2026-10-15; cloud build, re-verified on Windows)
- Studio drives LangGraph executors that run in their own process. First connector: the original M1 executor.
  Files: `studio/connectors/{base,m1_legacy,m1_legacy_host}.py`, `studio/connected.py`; `otlp.py` correlation;
  store: `connectors` table + `runs.remote_run_id`; API `/api/connectors*`; run endpoints dispatch on `origin=connected`;
  legacy `api.py` gained read-only `GET /api/graph`. UI: `pages/ConnectedPage.jsx`, run page/fork dialog schema-driven.
- Decision: separate process + control API (not in-process import): no dependency coupling, legacy DB keeps its
  owner lock, original runtime environment (fidelity). Spans are rebuilt from the executor's checkpoint history.
- Verified in Linux cloud env (user's site-packages + pure-Python shims for compiled wheels): pytest 203 passed /
  10 skipped; `tests/studio/test_connected_*` (in-process + 3-process e2e); `scripts/verify_connected_ui.py` 10/10,
  0 browser errors; V1 UI script 10/10. web/studio built with esbuild in the cloud → rebuild with `npm run build`.
- Windows re-check (user machine, 23:05): `pytest -q tests/studio` 40 passed; `scripts/verify_connected_ui.py` 10/10 PASS, 0 browser errors.
- Details/limits: `docs/connected-executors.md`.

### Milestone 6 — studio-executor SDK for any LangGraph agent (2026-10-15, cloud; NOT yet re-run on Windows)
- `src/studio_executor/` (independent of m1_local): `StudioExecutor(graph, ...)` + dependency-free ASGI app +
  `serve`/`serve_async`. Run registry (SQLite optional), head tracking via `stream_mode="checkpoints"`, pause by
  stopping after a checkpoint event (next step not started), resume incl. `interrupt()` and failed runs, fork via
  `aupdate_state` (paused branch, same thread), replay. Fork fields restricted to LastValue channels. Token + no-Origin
  + loopback Host.
- Studio: connector kind `studio_executor` (`connectors/generic.py`, attach + token, token masked), dialog option.
- Independent example: `examples/langgraph_agent/support_agent.py` (AsyncSqliteSaver, deterministic/openai).
- Verified (cloud): pytest 211 passed / 10 skipped; `scripts/verify_sdk_ui.py` 6/6; connected + V1 UI scripts PASS.
- Guide: `docs/studio-executor-sdk.md`.

## Next actions (post-V1 backlog, in priority order)

0. `npm run build` (replace the cloud esbuild bundle); full `pytest -q`; try a real-model executor
   (`Real model` in the connect dialog); commit.
0b. Windows: `pytest -q`, `scripts/verify_sdk_ui.py`; run the example agent with `--mode openai`.
0c. Try the SDK on a real third-party LangGraph agent; consider packaging `studio_executor` separately (own pyproject).
1. Span-level replay (re-issue one LLM call with an edited prompt; show diff) — research requirement not yet met.
2. Parallel fan-out/join node semantics in the compiler (LangGraph supports it; schema currently forbids).
3. Optional export of Studio spans to Phoenix/OTLP; show legacy attribution results for external traces.
4. UI: drag-from-palette, undo/redo, design-time template reference checking.

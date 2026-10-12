# Agent Studio V1 — Delivery Report

Branch `feature/agent-studio-v1` (local commits only, nothing pushed). Evidence is in [V1_VALIDATION_REPORT.md](V1_VALIDATION_REPORT.md).

## 1. What was delivered

A local-first web application, **Agent Studio**, for building, running, observing and debugging multi-agent
workflows, plus read-only observation of agents built elsewhere:

- **Workflow builder**: React Flow canvas with five node types (Start, Agent, Tool, Condition, Output), a node
  inspector, live validation, save/version, delete.
- **Execution engine**: workflows compile to LangGraph `StateGraph`s with SQLite checkpoints. Agents call an
  OpenAI-compatible model (with a function-calling tool loop) or a clearly labelled deterministic script.
- **Observability**: every node, LLM call and tool call becomes a persisted span with parent/child links,
  input/output, timing, status, errors and stack traces. The UI provides a graph overlay, a timeline and a
  filterable span explorer.
- **Workflow debugging**: breakpoints, pause at the next node boundary, resume, cancel, retry of a failed
  node, checkpoint browser, **fork with edited state** and **replay from a checkpoint** into new runs with
  lineage, and a branch comparison view.
- **External agents**: an OTLP/HTTP ingest (`/v1/traces`, protobuf and JSON) turns any OpenTelemetry agent into
  a read-only trace. A bundled independent agent (`examples/external_agent/`) can be launched from the UI.
- **Persistence and restart**: workflows, runs, spans, lineage and checkpoints survive restarts. Runs that were
  active during a crash are marked `interrupted` and can be resumed.

## 2. Completion matrix (every mandatory module)

| Module / requirement | Status | Notes (limits are stated, not hidden) |
|---|---|---|
| A. Workspace: list, create, save, open/edit, run history, inspect runs, persist across restart | **WORKING** | Single local user. Each run stores a definition snapshot, so editing or deleting a workflow never alters history. |
| B. Visual builder: show nodes and edges, add/remove, connect, edit config, save/reopen, run from UI, live status | **WORKING** | 5 node types and 3 tools. Each non-condition node has exactly one outgoing edge: **no parallel fan-out/join**. Nodes are added by clicking the palette (no drag-and-drop from the palette). No undo. |
| C. Real multi-agent execution (Planner → Researcher (tools) → Reviewer → loop → Output) | **WORKING** | Verified with `gpt-4.1-mini` (real tool calling included). The deterministic provider is labelled in the run header, run list and spans. Missing key → explicit `MissingCredentials` failure. |
| D. Existing-agent integration: standard instrumentation + one executable local integration | **WORKING (constrained)** | OTLP ingest accepts any OpenTelemetry/OpenInference agent and marks the run read-only. One built-in launchable integration; no UI for registering arbitrary commands (by design for safety). No control channel into external agents. |
| E. Per-agent observability (identity, I/O, tool calls, model details, timestamps, duration, status, errors, parent-child; filter and drill-down) | **WORKING** | Model, latency and token usage are recorded for real calls. The Studio uses its own trace store; it does not send its spans to Phoenix (see deferred). |
| F. Timeline and execution graph with step inspection and run navigation | **WORKING** | Graph overlay with visit counts and inherited/not-run states; Gantt-style timeline; links between parent and child runs. |
| G. Pause / resume / replay / fork with preserved history and lineage | **WORKING for Studio workflows** | Pause and breakpoints act at node boundaries, not inside a model call. Fork may edit `input.*` and `nodes.<id>.output` (conditions are recomputed). Source runs are never modified (asserted in tests and in the real-model check). Replay restores state, but model calls are not deterministic, and the UI says so. A breakpoint fires once per node per run. **Span-level replay (re-issuing a single LLM call with an edited prompt) is NOT IMPLEMENTED.** External runs are trace-only by design. |
| H. Failure inspection: failed steps, errors and stack traces, I/O, compare branches, retry | **WORKING** | Retry = resume from the checkpoint before the failed node. Legacy failure-attribution research (benchmark, baselines) is **kept but not integrated into the Studio UI** → PARTIALLY WORKING for "retain attribution assets". |
| I. Persistence and restart recovery | **WORKING** | Verified by killing the real server process (e2e test) and by an engine restart test (paused and crashed runs both resume). |
| J. UI/UX (Sim-inspired layout: sidebar, canvas, config panel, controls, history, trace inspection) | **WORKING** | Light/dark follows the OS. English UI. Tested at 1440×900 in Chromium only. |

Definition-of-Done checklist: app starts with documented commands ✅ · UI opens ✅ · a multi-agent workflow
executes (real model) ✅ · real execution data reaches the observability UI ✅ · per-agent I/O inspectable ✅ ·
runs persist ✅ · workflows edited and saved ✅ · genuine checkpoint resume/fork ✅ · fork lineage preserved ✅ ·
an external, independently defined agent integrated through a documented interface ✅ · failures visible ✅ ·
survives restart ✅ · integration and e2e tests executed ✅ · limitations documented ✅.

## 3. Architecture

```
frontend/ (React 18, @xyflow/react 12, Vite 6) ──build──▶ web/studio/ (served by FastAPI)
                         │ fetch /api/* (CSRF header), polling
src/m1_local/studio/
  app.py          FastAPI: workflows, runs, spans, checkpoints, control, compare, integrations, OTLP, SPA
  schema.py       workflow definition model + structural validation
  engine.py       definition → LangGraph StateGraph; spans; pause/resume/cancel; fork/replay (new thread)
  runtime_support templates ({{input.x}}, {{nodes.id.output}}, {{x?}}), tools, providers (openai, deterministic)
  store.py        SQLite: workflows, runs (lineage, capabilities, definition snapshot), spans, settings
  otlp.py         OTLP/HTTP ingest → external read-only runs
  integrations.py launch the external agent subprocess (reports back via OTLP only)
data/studio/      studio.sqlite + checkpoints.sqlite (LangGraph AsyncSqliteSaver)
examples/external_agent/research_agent.py  independent OTel-instrumented Analyst→Critic program
```

Key decisions:
- **Capability model per run** (`execute`, `checkpoint`, `pause_resume`, `fork`, `replay`, `retry`). The UI shows it
  and the backend enforces it (external runs return 409 for control calls).
- **Fork and replay write to a new LangGraph thread**, seeded with the checkpoint state via `aupdate_state(as_node=…)`.
  The source thread is never written, which gives a strong guarantee that history is preserved.
- **Own normalized trace store** instead of depending on Phoenix: the UI needs parent/child spans, checkpoint links
  and lineage that Phoenix does not model, and the app stays usable without a second service.
- **Deterministic provider** with scripted responses and scripted tool calls exercises the same code paths as real
  calls, so replay semantics and failure handling can be tested offline and cheaply.

### Reused vs replaced legacy components

| Legacy | Decision |
|---|---|
| LangGraph + `AsyncSqliteSaver`; checkpoint, fork and replay patterns from `runtime.py` | **Reused** (re-implemented generically in `studio/engine.py`) |
| Fixed Magentic-One graph (`graph.py`), `RuleBasedGateway` | **Replaced** in the product by a user-defined workflow compiler and the labelled deterministic provider |
| AutoGen `MultimodalWebSurfer` | **Not used** by the Studio (heavy, private-API dependency); still available in the legacy `serve` app |
| Phoenix as the primary viewer | **Replaced** by the Studio trace UI; Phoenix and the legacy observation app remain available |
| Vanilla-JS pages (`web/index.html`, `web/observation.html`) | **Kept** for the legacy apps; the Studio has a new React UI |
| Host/Origin/CSRF boundary, `build_info` | **Reused** |
| Failure-attribution code, frozen benchmark protocol/results | **Kept untouched** (not integrated) |

## 4. Startup

Requirements: Windows 11, Python 3.11 (`scripts/install.ps1` creates `.venv`). Node.js is only needed to rebuild the UI.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1   # first time only
$env:OPENAI_API_KEY = '<your key>'          # optional; set in your own shell, never commit it
.\scripts\start-studio.ps1                  # foreground; open http://127.0.0.1:8100 ; Ctrl+C stops
```

Options: `-Port 8100`, `-DataDir data\studio`. Environment: `OPENAI_BASE_URL` (any OpenAI-compatible endpoint),
`STUDIO_DEFAULT_MODEL` (default `gpt-4.1-mini`). Base URL and model can also be changed in **Settings**. The key
is read from the environment only and is never stored or returned by the API.
Equivalent command: `.venv\Scripts\python.exe -m m1_local.cli studio --port 8100`.

## 5. Demo script

1. **Workflows** → *Research → Review loop*. Inspect nodes in the right panel.
2. **Run** → provider *Real model* (or *Deterministic script*) → optionally tick the *Reviewer* breakpoint → *Start run*.
   Nodes turn blue, then green; with a breakpoint the run pauses at Reviewer. Press **Resume**.
3. Click **open** on the run chip → **Graph** (×N loop counts) → **Timeline** → **Spans**: select `llm:openai` to see the
   messages, model and token usage; select `tool:*` to see arguments and results.
4. **Checkpoints & fork** → pick *after reviewer* → **Fork with edits** → change `nodes.reviewer.output`
   (for example `REVISE: …` ⇄ `APPROVED`) → *Create branch and run*. The new run shows `fork of run_…`, inherited
   nodes, and a different path. **Compare** shows the per-node differences. The parent run is unchanged.
5. **Replay from here** on an earlier checkpoint shows state restoration. With the real model the outputs may differ;
   this is intended and is explained in the dialog.
6. Failure: open *Calculator router*, run with `1/0` → red node, error banner, **Retry failed node**.
7. **Integrations** → *Launch agent process* (deterministic or openai; optionally `fail_tool=true`) → **Open trace →**:
   a read-only external run (span tree, I/O, errors with stack trace); Checkpoints & fork are disabled.
8. Any other OTel agent: export to `http://127.0.0.1:8100/v1/traces` (snippet on the Integrations page).

## 6. Known limitations

- Single process, single user, loopback only; no authentication. OTLP ingest accepts loopback clients only.
- No parallel branches, sub-workflows or human-input nodes; condition operators are simple string/number comparisons.
- Pause granularity is the node boundary. Breakpoints are released after their first stop in a run.
- At most 2 concurrent runs; no queue UI.
- Template references are not type-checked at design time; missing references fail at run time with a clear error.
- `http_get` blocks private addresses unless `STUDIO_ALLOW_PRIVATE_HTTP=1` is set. It has not been exercised against
  live web pages in tests.
- External integrations are observe-only; the Studio cannot pause or fork external agents.

## 7. Deferred (not in V1)

Span-level replay; parallel fan-out/join; exporting Studio spans to Phoenix/OTLP; registering custom executable
integrations from the UI; showing legacy failure-attribution results in the Studio; undo/redo and drag-and-drop from
the palette; multi-user/auth; packaging the legacy WebSurfer as a Studio tool node.

## 8. Validation summary

202 automated tests pass (3 opt-in skips). The browser flow passes 10/10 with zero console or network errors.
Model-backed checks pass 5/5 (real multi-agent run, fork of a real run with a changed path, replay, real tool calling,
real-model external agent), and a mid-run pause/resume was verified with the real model. Details, commands and
evidence paths: [V1_VALIDATION_REPORT.md](V1_VALIDATION_REPORT.md).

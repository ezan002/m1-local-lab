# Agent Studio V1 — Validation Report

Date: 2026-10-12 · Branch `feature/agent-studio-v1` · Windows 11, Python 3.11.9, Node 24.21 (build only), Chromium 139 (Playwright 1.54).

Legend: **PASS** = executed and the result was checked; **NOT RUN** = not executed; **BLOCKED** = cannot run here.
"Deterministic" means the scripted provider (`provider=deterministic`): the code paths are real (LangGraph, SQLite,
HTTP, spans), but agent text comes from a script. That is labelled in the UI and API, and it is **not** evidence of model quality.

## 1. Verified with real execution (real model and/or real processes)

| What was tested | How | Expected | Actual | Evidence | Result |
|---|---|---|---|---|---|
| Multi-agent workflow with a real model | `scripts/validate_v1_real_model.py`: own server, Research→Review run, `provider_override=openai` | completes; 3 llm spans with model and usage | completed, `gpt-4.1-mini-2025-04-14`, token usage recorded | `docs/evidence/v1/real-model/receipt.json` check 1 | PASS |
| Fork of a real-model run changes the path | same script: fork after the first reviewer checkpoint, verdict flipped (`APPROVED` → `REVISE: …`) | new run with lineage; different downstream path; source unchanged | path `needs_revision→researcher→reviewer→needs_revision→final` (real model re-ran); source spans and checkpoint ids identical before/after | check 2 | PASS |
| Replay from a checkpoint ≠ deterministic reproduction | replay after planner | planner restored, not re-run; researcher onward re-executed | first executed node `researcher`; output differs from source (expected for model calls) | check 3 | PASS |
| Real tool calling (OpenAI function calling) | agent with `calculator`, asked for 48271*9137-17 | tool span with call id; answer uses the tool result | tool span `call_og4t…`, result 441052110, output `441052110` | check 4 | PASS |
| External agent with a real model | launched `examples/external_agent/research_agent.py --mode openai` from the API | exit 0; read-only external run; llm spans | exit 0; 6 spans; `llm.model_name=gpt-4.1-mini`; fork capability false | check 5 | PASS |
| Pause mid-run with a real model | daily server (`data/studio`), pause requested while the planner LLM call ran | pauses before the next node; resume completes | paused before `researcher`; resume → completed (run `run_03d5c883aa06`) | console transcript in this session; run persisted in `data/studio` | PASS |
| Real server process, HTTP API, external agent subprocess, server restart | `tests/studio/test_studio_e2e_server.py` (deterministic provider) | edits persist; runs/spans/lineage identical after the process is killed; paused run resumes after restart | all assertions pass | pytest | PASS |
| Browser UI end-to-end | `scripts/verify_studio_ui.py` (real Chromium, real server, deterministic provider) | 10 user flows; no console/page/network errors | 10/10 PASS; console 0, page errors 0, failed requests 0, HTTP ≥400: 0 | `docs/evidence/v1/ui/receipt.json` + 10 screenshots | PASS |

UI flows covered: list → new workflow → add a Tool node → drag-connect two edges → edit configs → save (verified via API)
→ run with live node status → span drill-down → timeline → fork with an edited agent output (downstream result changes to
`Words: 3 | one two three`, lineage persisted) → inherited nodes shown → reload persists → failed run (`1/0`) highlighted
with error and retry hint → external agent launched from Integrations, shown read-only with fork disabled.

## 2. Verified with deterministic local execution (automated, `pytest`)

Command: `OPENAI_API_KEY= RUN_REAL_MODEL_TESTS=0 .venv\Scripts\python.exe -m pytest -q` → **202 passed, 3 skipped**
(final run, includes 32 Studio tests and the unchanged legacy suite). The 3 skips are the 2 opt-in real-WebSurfer tests and 1 symlink test the OS does not allow.

| Area | Tests (file) | Highlights |
|---|---|---|
| Schema / validation | `test_studio_schema.py` (11) | structural errors (start count, branch edges, unknown nodes, missing tool or prompt, non-terminating cycles), template errors, calculator sandbox, http_get refuses private addresses |
| Engine (real LangGraph + SQLite checkpointer) | `test_studio_engine.py` (14) | nested spans and loop guard; fork changes the path and leaves the source byte-identical; replay reproduces deterministic output; invalid forks rejected (terminal, unknown/forbidden fields, bad checkpoint, replay with changes); breakpoint pause/resume; pause request; tool failure + retry re-executes; condition routing; missing credentials fail explicitly; start defaults; restart recovery (paused + crashed→interrupted, both resumed); cancel closes spans, then resume |
| HTTP API + OTLP | `test_studio_api.py` (6) | host/origin/CSRF boundary; CRUD + validation + persistence across app restart; run/inspect/fork/compare via API; error surfacing; OTLP **protobuf from the real OTel SDK**: idempotent, read-only (resume/pause/fork 409); failures with stack traces; malformed protobuf/JSON → 400 |
| Server e2e | `test_studio_e2e_server.py` (1) | see §1 |

## 3. Verified using mocks only

- The legacy test suite (pre-existing, unchanged) still uses `RuleBasedGateway` and Fake Phoenix clients. Those tests passed in the same run.
- The Studio tests use no mocks, apart from one monkeypatch that slows the deterministic provider so cancellation can be tested.

## 4. Negative / failure tests

| Case | Where | Result |
|---|---|---|
| Invalid node configuration / graph | schema tests; API `PUT` → 422 with error list; editor shows issues and disables Run | PASS |
| Missing credentials | engine test (key removed) → `MissingCredentials` on `planner`, no output | PASS |
| Runtime/tool exception | `1/0` → `ToolError` on `calc` with traceback (API, UI, e2e) | PASS |
| Agent-side tool error | failing tool inside an agent loop: tool span `error`, error fed back to the model as a tool message, run continues (`test_agent_tool_error_is_returned_to_model_and_recorded`); external agent `--fail-tool` marks the run failed | PASS |
| Invalid checkpoint reference | engine KeyError; API 404 | PASS |
| Unsupported replay requests | terminal checkpoint 409; external runs 409; pre-first-node checkpoint rejected | PASS |
| Malformed / incomplete trace data | malformed protobuf/JSON 400; spans without ids rejected and listed | PASS |
| Backend restart | e2e (process killed), engine restart-recovery test, browser CSRF refresh after restart (found and fixed during browser testing) | PASS |
| External service unavailable | http_get to an unresolvable host → ToolError (code path); OpenAI errors wrapped as ProviderError | code-supported, NOT RUN against a live outage |

## 5. Not tested / limitations of the evidence

- `http_get` against real public web pages: NOT RUN (only refusal of private addresses is tested).
- Concurrent runs (2 slots) under load: NOT RUN.
- UI tested at 1440×900 in Chromium only; no Firefox/Safari, no small screens.
- Pausing *inside* an LLM call is not supported by design (node boundary only).
- Breakpoints fire once per node per run; in loops, later visits are not re-paused (documented).
- No accessibility audit.

## 6. Known failures

None open at the end of this session. Defects found by validation and fixed in this session:
`fitView` before async load, stale cached `index.html`, CSRF token not refreshed after a server restart,
child spans counted as node visits (UI and compare API), start-node defaults not propagated to `input.*`
(found by the real-model tool check), spans left `running` after cancellation.

## 7. External blockers

None. A real OpenAI key was available in the user environment. If it is missing, model-backed checks
report BLOCKED (`validate_v1_real_model.py` exits 2) and the deterministic tests still run.

## Re-running

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/studio          # ~20 s, offline
.\.venv\Scripts\python.exe scripts\verify_studio_ui.py         # ~40 s, real Chromium, offline
.\.venv\Scripts\python.exe scripts\validate_v1_real_model.py   # real model calls (~12 small requests)
```

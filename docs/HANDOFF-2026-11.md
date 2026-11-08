# Handoff · 2026-11-07

Status of Agent Studio after the progress review with the advisor (meeting transcript of 2026-10-19/20), and the agreed
plan. A new conversation can resume from this file.

## Advisor's requests (from the meeting)

1. **Slim the UI**:
   - Hide Workflows and Runs (keep the code).
   - Hide the Integrations page (keep the test launcher).
   - Move "import external traces" to the top of **External traces** ("import on top, list below", like Connected
     agents).
   - Support importing a trace **file**, not only the OTLP endpoint.
2. **Re-check every remaining feature**, re-test it, and answer the open questions below with evidence.
3. Run **M1 with a real model** under Connected agents (it was shown with the deterministic gateway).
4. Later, after a joint review with 俊杰老师: new cards in the run view, e.g. data flow / control flow (how a piece of
   data moves through the nodes and how it changes).

## Open questions and verified answers

| Question | Answer (verified in code) |
|---|---|
| Does connected LangGraph support depend on instrumentation? | No. Fork and replay use LangGraph's own checkpoints: one per node, written by the checkpointer. Requirements: the graph is compiled with a checkpointer, and the wrapper declares which state fields are editable (`ForkField`). OpenTelemetry is not involved. |
| Where does an external trace's graph come from? | From OTLP span parent/child links (`parent_span_id`). Ingest is the Studio's own code (`studio/otlp.py`), using OpenInference attribute conventions, not Phoenix code. Non-OTel raw trajectories must be converted first. |
| Fork can edit earlier nodes? | Yes (`engine._apply_changes`): input and any executed node's output can be edited. Execution resumes only from the checkpoint's `next` node; earlier nodes are not re-run. **Decision: plan A** (below). |
| Integrations test agent model | Default `deterministic`. `openai` mode uses `gpt-4.1-mini` (`STUDIO_DEFAULT_MODEL`), not GPT-4. |
| M1 under Connected | It was launched with Deterministic (RuleBasedGateway). It also supports a real model (`m1_legacy_host --model openai`). |
| Span count 11 vs 10 graph nodes | The extra span is the workflow root span (named after the workflow). It is not a graph node. |
| Still to write up | Span vs step (a span may contain several LLM or tool calls); inconsistent top-level parent (workflow root vs the agent's own root such as "research session"); ordering and indentation of parallel spans in the external example; Agent and Tool node compatibility (OpenAI-compatible chat API or deterministic; tools calculator, http_get, word_count; condition operators); Settings scope (Studio workflow agents plus the Integrations test agent in openai mode; connected agents use their own models); meaning of Run input/output; per-step input/output view. |

Corrections to statements made in the meeting:
- LangGraph does **not** emit OpenTelemetry by default.
- External traces are **not** built on Phoenix code.
- The Integrations OTLP endpoint is a live receiver, not only a history import.

## Decision: fork semantics = plan A

At a checkpoint "after node X", a fork may edit **only X's output** (for connected agents: the declared fields that
X wrote at that step). To change an earlier node, fork from that node's checkpoint. The UI states which node execution
continues from. Replay is unchanged: it continues from the selected checkpoint with unchanged state.

## Plan

Progress (2026-11-07): **1 done, 2 done, 3 done** (`docs/advisor-qa-2026-11.md`; also fixed external span ordering, `tree_order`) (tests/studio + SDK tests 49 passed; verify_studio_ui 10/10, verify_sdk_ui 6/6, verify_connected_ui 10/10). 4 done by the user on Windows (M1 + qwen3:8b via Ollama, run + fork OK); M1 forks can now also edit `next_agent` (route). 5 done: Codex session converter (`studio/converters.py`), imported from External traces; sample in user's data/codex-samples. Next: 6 (regression + demo video), Compare screenshots from the user (route fork + replay control) for the Q&A doc.

| # | Work | Owner | Done when |
|---|---|---|---|
| 1 | Hide Workflows, Runs and Integrations behind a flag; Connected agents becomes the default page; import area on External traces (endpoint info + file upload) | Claude | Sidebar shows only Connected agents, External traces and Settings; uploading an OTLP JSON file shows the trace |
| 2 | Fork plan A for Studio workflows and connected executors | Claude | Earlier fields are not editable; tests updated and passing |
| 3 | Q&A document answering every open question, with code references and screenshots | Claude writes, user reviews | Every question has an answer |
| 4 | Real-model M1 through Ollama's OpenAI-compatible API (`OPENAI_BASE_URL=http://localhost:11434/v1`, model `qwen3:8b`) | Claude configures, user runs on Windows | One run and one fork with the real model |
| 5 | Converter from raw trajectories to OTLP (first format: Codex session JSONL or SWE-agent trajectories) | Claude; user provides a sample | Imported trajectory shows a span tree |
| 6 | Full regression, regenerate the demo video for the new UI | Claude | Tests pass; video matches the UI |
| 7 | Review with the advisor and 俊杰老师; decide the data-flow / control-flow cards | User | Next requirements agreed |

User items:
- Contract renewal: expired 10/15; the advisor will forward a contact. Report the days for Oct and Nov together.
- Schedule the next meeting: weekend or Tuesday.
- Commit the code.

## Environment notes (cloud sessions)

- PyPI, npm and GitHub are blocked in the cloud container. The previous session bootstrapped from the user's zipped
  `.venv` site-packages and `frontend/node_modules`, plus pure-Python shims, and built the UI with esbuild.
- A new cloud session must repeat this: zip `.venv\Lib\site-packages` and `frontend\node_modules` again, or work
  through the linked computer.
- After any UI change on Windows, run `cd frontend; npm run build` so `web/studio` is consistent: index.html and its
  JS/CSS must come from the same build.
- Connected executors keep their token in the Studio DB. Restart agents with the same `STUDIO_EXECUTOR_TOKEN`, or
  delete and re-add the card.

## 2026-11-08 meeting (group review) and follow-up

- Asked: a **Steps** view next to Spans (finer than spans, numbered 1..N, same fields as spans); repo on GitHub that a
  student can clone and run with the history data; answers on span vs step and on where the graph comes from.
- Done: `studio/steps.py` + `GET /api/runs/{id}/steps` + Steps tab (tests `tests/studio/test_steps.py`). A step = one
  model call or tool call (outermost; nested file changes fold in; the same call recorded by two instrumentations is
  listed once) or a graph node without calls (start, condition, output, uninstrumented node). Real data: M1 26 spans
  -> 3 steps, Codex 10 -> 7, Studio workflow 10 -> 6.
- Done: `demo-data/` (built by `scripts/build_demo_data.py`, tokens scrubbed, no real Codex sessions) and
  `m1-local studio --demo` / `start-studio.ps1 -Demo`; README quick start for any OS.
- Not changed: next_agent route editing (the advisor questioned its value; kept, not demoed).
- Cloud/VM note: the linked computer's Linux VM can pip/npm install (PyPI and npm reachable); the cloud container
  cannot. Build the UI there with `npx vite build`; Playwright browsers are only in the cloud container.

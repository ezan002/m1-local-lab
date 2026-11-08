# Agent Studio walkthrough video

| File | What |
|---|---|
| [agent-studio-demo.mp4](agent-studio-demo.mp4) | Detailed walkthrough, about 20 minutes, 1920×1080, 30 fps, H.264, Chinese captions, 11 chapters with MP4 chapter markers |
| [agent-studio-demo.srt](agent-studio-demo.srt) | The same captions as a subtitle file (UTF-8, 199 entries, including chapter titles), timed to the video; regenerated on every build, or with `make_video.py <recording> --srt-only` |
| [agent-studio-preview.gif](agent-studio-preview.gif) | ~45 s README preview, 960 px |
| [agent-studio-preview.mp4](agent-studio-preview.mp4) | Same preview as MP4, 1280 px |
| [COVERAGE.md](COVERAGE.md) | Generated coverage list: every live check, with chapter start times and run ids |
| [demo-manifest.json](demo-manifest.json) | Recording metadata: chapters, all captions, step results, browser errors |

The video shows each control and explains what it does. The control being described is outlined with an orange
box and the cursor moves to it, while the caption explains it.

## Chapters

| # | Chapter | What is explained (each item is highlighted on screen) |
|---|---|---|
| 1 | Interface tour | The six sidebar entries; the "no model key" notice and the build label; the New workflow button; the fields on a workflow card |
| 2 | Settings | API key handling (`OPENAI_API_KEY`, never stored or returned); Base URL (any OpenAI-compatible endpoint); Default model; the Runtime fields: build, OTLP endpoint, and runs recovered at start |
| 3 | Workflow editor | The top bar (back, name, version and save state, delete, Save, Run); the five node types in the palette; tips; the canvas and the true/false outputs of a condition; zoom controls; the minimap; workflow settings (description, default provider, default model, template syntax). Per node type: **Start** (inputs); **Agent** (label, role, system prompt, prompt template, provider, model, temperature/max tokens, tools, max tool rounds, scripted response, scripted tool calls); **Condition** (left, operator list, right, max true visits loop guard); **Output** (template) |
| 4 | Run, breakpoints, live status | The run dialog: inputs, the 3 provider choices, breakpoints. A breakpoint before *Final brief*; the run strip; ×3 on Researcher/Reviewer explained (the scripted reviewer always says REVISE, and `max_true_visits=2` forces the loop to end); the paused node; Resume in the editor; "Selected run · last execution" in the inspector |
| 5 | Run detail | Breadcrumb, status, run id, provider badge, definition snapshot, capability chips. **Resume** → completed. Graph legend; clicking a node jumps to its span. Timeline columns. Spans: name, kind and status filters (shown with the `pause` span), detail fields, the model messages of an LLM span, Attributes |
| 6 | Checkpoints, fork, compare, replay | The checkpoint list (#, after, → next, forkable/parent/paused). Replay vs Fork. The checkpoint state. Fork: Reviewer output set to `APPROVED` → lineage, inherited nodes, the condition re-evaluated, Final reached directly. Compare (select or paste a run id, executed paths, per-node verdicts). Run input/output. Back to the source run, which lists its branches. Replay from *after Planner* → Compare, with the caption written from the real result (`same`) |
| 7 | New workflow | Default nodes; adding a Tool shows **1 issue(s)** and Run is disabled; choosing a tool and templated arguments; edge replacement when wiring; save (3 edges checked through the API); run; the Tool node's real output |
| 8 | Routing, failure, retry | Calculator router: `12 * (7 + 5)` takes the *Large result* branch and *Small result* is shown faded. `1/0`: red node, Retry from failure, error box, Inspect failing span, the failing input. **Retry failed node** → still fails, because the input is still `1/0`; the caption is written from the observed status |
| 9 | Connected LangGraph agents | Every option in the connect dialog (agent type, name, Studio-started vs attach, URL, model, port). The Studio starts the **M1 executor** as a separate process. The card: status, pid, graph, capabilities, editable fields, notes, declared inputs, browsing the executor's own runs. M1 run (Coder ok, WebSurfer not run, the executor's own `m1.*` OTLP spans). Attach the **SDK agent** (URL and token) → run (loop ×2) → executor-side checkpoints and editable fields → fork `review=APPROVED` → Compare. Connected runs table with lineage. **Stop executor** → history still readable offline |
| 10 | Integrations and external traces | Launch card: badges and the parameters Topic, Mode and Simulate tool failure. Launch → trace-only run: every capability ✕, Checkpoints disabled, span tree, OpenInference attributes. Relaunch with `fail_tool=true` → failed trace, error and stack trace. Critic absent, checked from the real spans. OTLP endpoint card and snippet; External traces list |
| 11 | Runs | Lists **only Studio workflow runs** (connected runs are under Connected agents, external traces under External traces). Search, status filter, the Kind column (`deterministic script`, `real model`, `no-llm`, fork of / replay of), Duration, Spans, Error |

## How it is made (and why it can be trusted)

- `scripts/demo/record_demo.py` starts a **fresh Agent Studio server** and the **independent example LangGraph agent**
  (`examples/langgraph_agent/support_agent.py`, served by the studio-executor SDK in its own process). Each run uses
  new temporary data directories and free ports.
  - The M1 executor is started from the UI during chapter 9, through "The Studio starts it".
  - Chromium is driven by Playwright and recorded with the Chrome DevTools screencast: 1280×720 CSS px at device
    scale 1.5, which gives 1920×1080 frames.
  - The cursor, click ripples and the orange highlight box are drawn into the page. They have `pointer-events: none`,
    so they never affect what is clicked.
- **Every step is asserted** against the running UI, and where useful against the API. Examples: node status and visit
  counts; fork lineage and `fork_changes`; saved edges; `finalize`/`final` actually executed; the failed external
  span.
- Captions that depend on a result (the replay comparison, the status after retry, whether Critic ran in the failed
  trace) are **written from the observed result**, not assumed.
- When a step fails, the chapter stops. The failure is captioned in red in the video, a screenshot is saved, the end
  card shows ✗, and COVERAGE.md lists the error and traceback. The next chapter starts from a reloaded page.
- `scripts/demo/make_video.py` uses FFmpeg to add:
  - title and chapter cards;
  - burned-in captions, shown at the top when the highlighted control is in the lower part of the screen;
  - a chapter label in the sidebar's empty area;
  - cross-fades and MP4 chapter markers;
  - an end card listing every step result.

  Chapter footage is **real time**: nothing inside a chapter is sped up or cut.
- **No model calls.** Studio workflows use the `deterministic` provider, which uses scripted agent responses and is
  labelled in the UI. The M1 executor uses its offline RuleBasedGateway, the SDK agent runs in its deterministic mode,
  and the OTLP example runs with `mode=deterministic`. `OPENAI_API_KEY` is removed from all child processes.

## Regenerate

Requirements:
- the project `.venv` with Playwright Chromium (`python -m playwright install chromium` if missing);
- `ffmpeg` and `ffprobe` on PATH, built with libx264 and libass. On Windows: `winget install Gyan.FFmpeg`, then open a
  new terminal.

```powershell
.\.venv\Scripts\python.exe scripts\demo\build_demo.py              # record + build -> docs\demo\
.\.venv\Scripts\python.exe scripts\demo\build_demo.py --pace 1.3   # 30 % slower pauses, captions and typing
.\.venv\Scripts\python.exe scripts\demo\build_demo.py --headed     # watch the browser while it records

# stages separately; --only records selected chapters (useful while editing captions)
.\.venv\Scripts\python.exe scripts\demo\record_demo.py --only editor,run
.\.venv\Scripts\python.exe scripts\demo\make_video.py data\demo\<timestamp> --out docs\demo
```

Chapter keys: `tour, settings, editor, run, detail, fork, build, failure, connected, external, runs`. The `detail`
and `fork` chapters use the run created in `run`.

Exit codes: `0` means every step passed. `2` means the video was built but some steps failed; they are visible in the
video and listed in COVERAGE.md. `1` means no recording could be made.

A complete run takes about 20 minutes of recording plus roughly 25–40 minutes of encoding on a 2-core machine; it is
faster on a desktop. Raw frames go to `data/demo/` (git-ignored, about 1 GB per full recording; delete old ones).

To edit the walkthrough, change `record_demo.py`. Each chapter is a plain function.
- `d.point(locator, "caption")` highlights a control and explains it.
- `d.say(...)` shows a caption without a highlight.
- `highlight=True` adds a caption to the README preview.
- Reading time per caption is `0.9 s + 0.095 s × characters`, scaled by `--pace`.

## Not in the video, and why

| Feature | Reason |
|---|---|
| Real model calls (OpenAI-compatible) | Must be reproducible offline and free. Deterministic runs are labelled; real-model runs are covered by `scripts/validate_v1_real_model.py`. |
| Third-party `local-deep-researcher` | Needs Ollama and web search on the recording machine, and one run takes minutes. |
| Pausing a run *while it is executing* | Deterministic runs finish in milliseconds. Pausing is shown through a breakpoint instead; live pause is covered by the tests. |
| Deleting workflows or connectors | Explained on screen but not clicked, to keep the walkthrough's data intact. |
| Phoenix and attribution research tools | Not part of the Agent Studio product path. |

## Problems found while making the videos (fixed, not hidden)

1. **Tool nodes counted twice in run graphs.** A single execution showed as “×2” because the node span and the
   tool-call span inside it share the same kind and node.
   - Fix: `nodeStatusFromSpans` in `FlowNode.jsx`.
   - Guard: chapter 7 asserts that the Tool node shows no “×”.
2. **Completed run with a stale graph.** The run page fetched the run and its spans in parallel, so the last poll
   could show “completed” with the final node missing.
   - Fix: fetch the run first, then the spans (`RunDetailPage.jsx`).
   - Guard: chapters 6 and 9 assert that `final`/`finalize` show `ok`.
3. **A wrong caption in the first (2:35) version.** It said the Runs page lists every run, including connected and
   external ones. The code (`RunsPage origin="studio"`) lists only Studio workflow runs. Corrected in chapter 11; the
   first version was replaced.
4. Problems in the recording script itself during development:
   - clicking a node that was outside the visible canvas, which accidentally hit the palette and added a node;
   - assuming every tool error has a stack trace.

   They were caught by the assertions and shown as red failure captions. Fixed: nodes are panned into view before
   they are used, pages are reloaded between chapters, and the stack-trace caption depends on what is actually shown.

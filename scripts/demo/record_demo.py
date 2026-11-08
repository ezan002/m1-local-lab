"""Record the detailed Agent Studio walkthrough against the real, running product.

What it does
  1. Starts a fresh Agent Studio server (temporary data dir, free port) and the independent example LangGraph agent
     (examples/langgraph_agent/support_agent.py, studio_executor SDK) in its own process. The original M1 executor is
     started from the UI during the recording ("The Studio starts it").
  2. Drives the real UI with Playwright (Chromium, 1280x720 CSS px at device scale 1.5 = 1920x1080 frames) and records
     the browser with the Chrome DevTools screencast. A cursor, click ripples and a spotlight box around the control
     being explained are drawn into the page; the explanation is recorded as a caption for make_video.py.
  3. Every step is checked against the real UI (and, where useful, the Studio API). A failed step is recorded with its
     error, left visible on screen, captioned as a failure in the video and listed in the coverage report. Captions
     whose content depends on an outcome (retry result, replay comparison, failed trace shape) are written from the
     observed result, not assumed. Nothing is mocked; there is no fallback to static pages or pre-recorded results.

Output  (default data/demo/<timestamp>/, git-ignored)
  frames/*.jpg + manifest.json    raw recording, chapter marks, captions, step results, browser errors
Then run scripts/demo/make_video.py <that dir> (or scripts/demo/build_demo.py, which runs both).

Model calls: none. Studio workflows run with the "deterministic" provider (scripted agent responses, labelled in the
UI), the M1 executor uses its offline RuleBasedGateway, the SDK agent runs in its deterministic mode and the OTLP
example agent runs with mode=deterministic. OPENAI_API_KEY is removed from every child process.

    python scripts/demo/record_demo.py [--out DIR] [--pace 1.0] [--headed]
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
VIEWPORT = {"width": 1280, "height": 720}
SCALE = 1.5                                   # 1280x720 * 1.5 = 1920x1080 frames, larger readable UI text
TOKEN = "demo-" + "k" * 24
QUESTION = "I was charged twice, how do I get a refund?"
M1_TASK = "Draft code for a UUID validation function"

OVERLAY_JS = r"""
(() => {
  if (window.__demoOverlay) return; window.__demoOverlay = true;
  const install = () => {
    const c = document.createElement('div');
    c.id = '__demo_cursor';
    c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;' +
      'transform:translate(-100px,-100px);transition:transform 0.02s linear;';
    c.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22"><path d="M3 2 L3 18 L7.5 13.8 L10.6 20.4 L13.4 19.1 ' +
      'L10.3 12.6 L16.4 12.4 Z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(c);
    const s = document.createElement('div');
    s.id = '__demo_spot';
    s.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;border:3px solid #ff9f1c;border-radius:9px;' +
      'box-shadow:0 0 0 9999px rgba(15,16,32,.22), 0 0 18px rgba(255,159,28,.75);opacity:0;' +
      'transition:left .25s ease,top .25s ease,width .25s ease,height .25s ease,opacity .2s ease;';
    document.documentElement.appendChild(s);
    window.__demoSpot = (r) => {
      if (!r) { s.style.opacity = '0'; return; }
      s.style.left = r.x + 'px'; s.style.top = r.y + 'px'; s.style.width = r.w + 'px'; s.style.height = r.h + 'px';
      s.style.opacity = '1';
    };
    document.addEventListener('mousemove', (e) => {
      c.style.transform = `translate(${e.clientX - 3}px, ${e.clientY - 2}px)`; }, true);
    document.addEventListener('mousedown', (e) => {
      const r = document.createElement('div');
      r.style.cssText = `position:fixed;left:${e.clientX - 18}px;top:${e.clientY - 18}px;width:36px;height:36px;` +
        'border-radius:50%;background:rgba(108,92,231,.35);border:2px solid rgba(108,92,231,.9);z-index:2147483646;' +
        'pointer-events:none;transform:scale(.3);opacity:1;transition:transform .45s ease-out, opacity .45s ease-out;';
      document.documentElement.appendChild(r);
      requestAnimationFrame(() => { r.style.transform = 'scale(1.4)'; r.style.opacity = '0'; });
      setTimeout(() => r.remove(), 600);
    }, true);
  };
  if (document.documentElement) install(); else document.addEventListener('DOMContentLoaded', install);
})();
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 60, headers: dict | None = None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, trust_env=False, timeout=2, headers=headers).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{url} did not become ready in {timeout}s")


class Screencast:
    """Chrome DevTools screencast: each frame is saved with its browser timestamp (seconds since epoch)."""

    def __init__(self, context, page, frames_dir: Path) -> None:
        self.dir = frames_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames: list[dict] = []
        self.cdp = context.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self._frame)

    def _frame(self, params: dict) -> None:
        name = f"{len(self.frames):06d}.jpg"
        (self.dir / name).write_bytes(base64.b64decode(params["data"]))
        self.frames.append({"file": name, "t": params["metadata"].get("timestamp") or time.time()})
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:  # noqa: BLE001 - the session may close while a frame is in flight
            pass

    def start(self) -> None:
        self.started = time.time()
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88, "maxWidth": 1920, "maxHeight": 1080,
                                               "everyNthFrame": 1})

    def stop(self) -> None:
        self.stopped = time.time()
        try:
            self.cdp.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001
            pass


class Demo:
    def __init__(self, page, base: str, pace: float) -> None:
        self.page, self.base, self.pace = page, base, pace
        self.chapters: list[dict] = []
        self.captions: list[dict] = []
        self.steps: list[dict] = []
        self.mouse = (VIEWPORT["width"] / 2, VIEWPORT["height"] / 2)
        self.ctx: dict = {}

    # ---- timing & annotations -------------------------------------------------------------------------------
    def wait(self, seconds: float) -> None:
        # page.wait_for_timeout (not time.sleep) keeps Playwright's event loop running, so frames keep arriving.
        self.page.wait_for_timeout(int(seconds * 1000 * self.pace))

    @staticmethod
    def read_time(text: str) -> float:
        return 0.9 + 0.095 * len(text)       # ~10 Chinese characters per second plus a short settle

    def chapter(self, key: str, title: str, subtitle: str) -> None:
        self.chapters.append({"key": key, "title": title, "subtitle": subtitle, "t": time.time()})
        print(f"\n== {title}", flush=True)

    def say(self, text: str, hold: float | None = None, highlight: bool = False, pos: str = "bottom") -> None:
        hold = self.read_time(text) if hold is None else hold
        self.captions.append({"t": time.time(), "text": text, "chapter": self.chapters[-1]["key"], "kind": "info",
                              "highlight": highlight, "pos": pos, "dur": hold * self.pace})
        self.wait(hold)

    def point(self, locator, text: str, hold: float | None = None, highlight: bool = False, pad: int = 5) -> None:
        """Spotlight one control, move the cursor to it and explain it in a caption."""
        locator = locator.first
        locator.scroll_into_view_if_needed()
        self.wait(0.15)
        box = locator.bounding_box()
        if box is None:
            raise RuntimeError(f"cannot point at an invisible element ({text[:30]})")
        x, y = max(2, box["x"] - pad), max(2, box["y"] - pad)
        w = min(box["width"] + 2 * pad, VIEWPORT["width"] - x - 2)
        h = min(box["height"] + 2 * pad, VIEWPORT["height"] - y - 2)
        self.page.evaluate("r => window.__demoSpot && window.__demoSpot(r)", {"x": x, "y": y, "w": w, "h": h})
        tx = box["x"] + min(box["width"] - 6, max(12, box["width"] * 0.85))
        ty = box["y"] + min(box["height"] - 4, max(8, box["height"] * 0.7))
        self.glide(tx, ty)
        pos = "top" if y + h > VIEWPORT["height"] * 0.74 else "bottom"
        self.say(text, hold=hold, highlight=highlight, pos=pos)
        self.page.evaluate("() => window.__demoSpot && window.__demoSpot(null)")
        self.wait(0.15)

    def ok(self, name: str, **evidence) -> None:
        self.steps.append({"chapter": self.chapters[-1]["key"], "step": name, "status": "PASS", "t": time.time(), **evidence})
        print("  PASS", name, flush=True)

    def fail(self, name: str, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {str(exc).splitlines()[0][:220]}" if str(exc) else type(exc).__name__
        self.steps.append({"chapter": self.chapters[-1]["key"], "step": name, "status": "FAIL", "t": time.time(),
                           "error": message, "traceback": traceback.format_exc()[-3000:]})
        self.captions.append({"t": time.time(), "text": f"未通过：{name} — {message}", "chapter": self.chapters[-1]["key"],
                              "kind": "error", "highlight": False, "pos": "bottom"})
        print("  FAIL", name, message, flush=True)

    # ---- human-like input -------------------------------------------------------------------------------------
    def move_to(self, locator, dx: float | None = None, dy: float | None = None) -> tuple[float, float]:
        locator = locator.first
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if box is None:
            raise RuntimeError("element has no bounding box")
        x = box["x"] + (dx if dx is not None else box["width"] / 2)
        y = box["y"] + (dy if dy is not None else box["height"] / 2)
        self.glide(x, y)
        return x, y

    def glide(self, x: float, y: float) -> None:
        dist = math.dist(self.mouse, (x, y))
        steps = max(6, min(40, int(dist / 18)))
        self.page.mouse.move(x, y, steps=steps)
        self.mouse = (x, y)

    def click(self, locator, pause: float = 0.35, **kw) -> None:
        self.move_to(locator, **kw)
        self.wait(0.15)
        self.page.mouse.down()
        self.page.mouse.up()
        self.wait(pause)

    def type(self, locator, text: str, delay_ms: int = 28) -> None:
        locator = locator.first
        self.click(locator, pause=0.1)
        locator.press("ControlOrMeta+a")
        locator.press_sequentially(text, delay=int(delay_ms * self.pace))
        self.wait(0.3)

    def choose(self, select_locator, value: str, caption: str | None = None) -> None:
        """Native <select> popups are not drawn in headless screenshots, so point at the select and set the value."""
        self.move_to(select_locator)
        select_locator.first.select_option(value)
        self.wait(0.5)
        if caption:
            self.point(select_locator, caption)

    def drag(self, src, dst) -> None:
        self.move_to(src)
        self.page.mouse.down()
        box = dst.first.bounding_box()
        tx, ty = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.page.mouse.move(tx, ty, steps=22)
        self.mouse = (tx, ty)
        self.page.mouse.up()
        self.wait(0.4)

    def zoom_at(self, locator, notches: int) -> None:
        """Zoom the graph around an element with the mouse wheel (React Flow zooms towards the pointer)."""
        self.move_to(locator)
        for _ in range(notches):
            self.page.mouse.wheel(0, -120)
            self.wait(0.25)
        self.wait(0.3)

    def open(self, path: str) -> None:
        """Navigate with a full reload: a hash-only change would keep unsaved editor state from a previous chapter."""
        self.page.goto(self.base + path)
        self.page.reload()
        self.page.wait_for_load_state("networkidle")

    def show_node(self, node_id: str) -> None:
        """Pan the graph (drag on empty canvas) until the node is fully inside the visible canvas."""
        n = self.page.locator(f"[data-testid=node-{node_id}]").first
        flow = self.page.locator(".react-flow").first
        nb, fb = n.bounding_box(), flow.bounding_box()
        if nb is None or fb is None:
            raise RuntimeError(f"node {node_id} is not rendered")
        m = 30
        if (fb["x"] + m <= nb["x"] and nb["x"] + nb["width"] <= fb["x"] + fb["width"] - m
                and fb["y"] + m <= nb["y"] and nb["y"] + nb["height"] <= fb["y"] + fb["height"] - m):
            return
        dx = fb["x"] + fb["width"] / 2 - (nb["x"] + nb["width"] / 2)
        dy = fb["y"] + fb["height"] / 2 - (nb["y"] + nb["height"] / 2)
        sx, sy = fb["x"] + fb["width"] / 2, fb["y"] + 14          # empty band at the top of the canvas
        self.glide(sx, sy)
        self.page.mouse.down()
        self.page.mouse.move(sx + dx, sy + dy, steps=24)
        self.page.mouse.up()
        self.mouse = (sx + dx, sy + dy)
        self.wait(0.4)

    def node_click(self, node_id: str) -> None:
        self.show_node(node_id)
        self.click(self.page.locator(f"[data-testid=node-{node_id}]"))

    def node_point(self, node_id: str, text: str, **kw) -> None:
        self.show_node(node_id)
        self.point(self.page.locator(f"[data-testid=node-{node_id}]"), text, **kw)

    def fit(self) -> None:
        self.click(self.page.locator(".react-flow__controls-fitview"), pause=0.6)

    def nav(self, label: str) -> None:
        self.click(self.page.locator(".sidebar nav").get_by_role("link", name=label, exact=True), pause=0.8)

    def api(self, path: str):
        return httpx.get(self.base + path, trust_env=False, timeout=10).json()


def field(scope, label: str):
    """A <label class="field"> whose caption starts with `label`."""
    page = scope.page if hasattr(scope, "page") else scope
    return scope.locator("label.field", has=page.locator(".field-label", has_text=re.compile("^" + re.escape(label))))


def node(page, node_id: str):
    return page.locator(f"[data-testid=node-{node_id}]")


def run_id_from_url(page) -> str:
    return page.url.split("/runs/")[1].split("?")[0]


# ======================================================================================================================
# Chapters. Each one is independent: a failure is recorded and the next chapter starts from a fresh URL.
# ======================================================================================================================

def ch_tour(d: Demo) -> None:
    page = d.page
    d.chapter("tour", "界面导览", "侧边栏六个入口分别做什么")
    d.open("/#/workflows")
    expect(page.get_by_text("Research → Review loop")).to_be_visible()
    d.point(page.locator(".brand"), "Agent Studio 在本机以单用户方式运行，工作流、运行记录和检查点都保存在本地 SQLite")
    sidebar = page.locator(".sidebar nav")
    explain = {
        "Workflows": "Workflows：在 Studio 里搭建的工作流，可视化编辑并运行",
        "Runs": "Runs：Studio 工作流的每一次执行，包括分叉（fork）和重放（replay）出来的分支",
        "Connected agents": "Connected agents：接入在自己进程里运行的外部 LangGraph 智能体，可暂停、恢复、分叉",
        "External traces": "External traces：外部程序通过 OpenTelemetry 上报的轨迹，只能查看，不能控制",
        "Integrations": "Integrations：外部接入方式——启动示例外部智能体、查看 OTLP 接收地址",
        "Settings": "Settings：模型服务地址、默认模型，以及运行时信息",
    }
    for label, text in explain.items():
        d.point(sidebar.get_by_role("link", name=label, exact=True), text)
    d.point(page.locator(".sidebar-foot .pill"),
            "左下角提示：没有配置模型 Key 时只能用 deterministic（脚本化回答）运行；配置后显示 OpenAI key configured")
    d.point(page.locator(".sidebar-foot .build"), "当前代码的 git 版本；dirty 表示有未提交的修改")
    d.ok("sidebar entries and status footer are present")
    d.point(page.get_by_role("button", name="New workflow"), "New workflow：新建一个工作流，默认带 Start → Assistant → Answer 三个节点")
    d.point(page.locator(".wf-card", has_text="Research → Review loop"),
            "工作流卡片：名称、描述、定义版本 v1、运行次数和最后更新时间；运行过后右侧会显示最近一次的状态", highlight=True)
    d.ok("workflow list renders the seeded workflows")


def ch_settings(d: Demo) -> None:
    page = d.page
    d.chapter("settings", "Settings 设置", "模型服务与运行时信息")
    d.open("/#/workflows")
    d.nav("Settings")
    expect(page.get_by_text("LLM provider (OpenAI-compatible)")).to_be_visible()
    d.point(page.locator(".card", has_text="LLM provider").locator(".badge").first,
            "API key 只从服务器环境变量 OPENAI_API_KEY 读取，不会保存，也不会通过接口返回；本次演示没有设置")
    d.point(field(page, "Base URL"), "Base URL：任何 OpenAI 兼容的接口都可以，例如本地的 vLLM 或 Ollama 网关")
    d.point(field(page, "Default model"), "Default model：工作流和节点都没有指定模型时使用的默认模型")
    d.point(page.get_by_role("button", name="Save"), "Save：保存上面两项（演示中不做修改）")
    kv = page.locator(".card", has_text="Runtime")
    d.point(kv.locator("dt", has_text="Build commit"), "Runtime：当前构建版本、是否有未提交修改、服务进程的启动时间")
    d.point(kv.locator("dt", has_text="OTLP endpoint"), "OTLP endpoint：外部智能体上报 OpenTelemetry 轨迹的地址")
    d.point(kv.locator("dt", has_text="Recovered at start"),
            "Recovered at start：服务重启时仍在运行的任务会被标记为 interrupted，之后可以从检查点恢复")
    d.ok("settings page shows provider settings and runtime information")


def ch_editor(d: Demo) -> None:
    page = d.page
    d.chapter("editor", "工作流编辑器", "顶栏、组件面板、画布与每种节点的配置项")
    d.open("/#/workflows")
    d.click(page.locator(".wf-card", has_text="Research → Review loop"))
    expect(node(page, "reviewer")).to_be_visible()
    top = page.locator(".editor-top")
    d.point(top.locator("a[title='All workflows']"), "返回工作流列表")
    d.point(top.get_by_label("Workflow name"), "工作流名称，可以直接修改")
    d.point(top.locator("span.muted").first, "定义版本和保存状态：有改动时显示 unsaved changes，保存后版本号加 1")
    d.point(top.locator("button.danger"), "删除工作流；已有的运行各自保存了定义快照，删除后仍可查看")
    d.point(top.get_by_role("button", name="Save"), "Save：保存当前定义；没有改动时不可点")
    d.point(top.get_by_role("button", name="Run", exact=True), "Run：运行这个工作流；存在校验问题时按钮会被禁用")
    palette = page.locator(".palette")
    d.point(palette.locator("h4").first, "左侧组件面板：点一下就在画布中央添加一个节点")
    items = {
        "Agent": "Agent：大模型智能体，可配置提示词、模型和允许调用的工具",
        "Tool": "Tool：直接调用一个工具（calculator / http_get / word_count），不经过大模型",
        "Condition": "Condition：条件判断，有 true（绿）和 false（红）两个出口，用来做分支和循环",
        "Output": "Output：用模板拼出最终结果",
        "Start": "Start：定义运行时需要填写的输入；每个工作流只能有一个，已存在时置灰",
    }
    for label, text in items.items():
        d.point(palette.locator(".palette-item", has=page.locator("strong", has_text=re.compile(f"^{label}$"))), text)
    d.point(palette.locator(".note"), "操作提示：从节点右侧的圆点拖线连接；选中一条连线后按 Delete 删除")
    d.zoom_at(node(page, "reviewer"), 2)
    d.point(page.locator(".canvas"),
            "画布：Planner → Researcher → Reviewer → “需要修改？”；条件为 true 时回到 Researcher，形成修改循环", highlight=True)
    d.node_point("needs_revision", "条件节点有两个出口：绿色 true 回到 Researcher，红色 false 进入 Final brief")
    d.fit()
    d.point(page.locator(".react-flow__controls"), "左下角：放大、缩小、适应画布")
    d.point(page.locator(".react-flow__minimap"), "右下角小地图：可拖动、可缩放，用来快速定位")
    ins = page.locator(".inspector")
    d.point(field(ins, "Description"), "右侧检查器：没有选中节点时，编辑整个工作流的设置。Description：说明文字")
    d.point(field(ins, "Default agent provider"),
            "Default agent provider：openai（真实模型）或 deterministic（脚本回答）；Agent 设为 inherit 时使用它，单次运行还可以覆盖")
    d.point(field(ins, "Default model"), "Default model：留空时使用服务器的默认模型")
    d.point(ins.locator(".note"), "模板语法：{{input.字段}} 引用输入，{{nodes.节点id.output}} 引用上游输出；加 ? 表示该值可能还不存在")
    d.ok("editor toolbar, palette, canvas and workflow settings explained")

    d.node_click("start")
    expect(ins.locator("h4", has_text="Inputs")).to_be_visible()
    d.point(ins.locator("h4", has_text="Inputs"),
            "Start 节点：每个输入有 name（模板里引用的名字）、label（显示名）和默认值；Add input 可以增加字段")
    d.node_click("planner")
    expect(field(ins, "Role")).to_be_visible()
    d.point(field(ins, "Label"), "Agent 节点。Label：画布上显示的名称")
    d.point(field(ins, "Role"), "Role：智能体的角色名，会出现在轨迹里，方便区分")
    d.point(field(ins, "System prompt"), "System prompt：系统提示词，规定这个智能体的身份和输出格式")
    d.point(field(ins, "Prompt"), "Prompt (template)：每次调用时发送的提示词，下方列出了可以引用的变量")
    d.point(field(ins, "Provider"), "Provider：inherit（跟随工作流设置）、openai 或 deterministic")
    d.point(field(ins, "Model"), "Model：留空表示使用默认模型")
    d.point(field(ins, "Temperature"), "Temperature：采样温度；Max tokens：单次回复的最大长度")
    d.node_click("researcher")
    expect(field(ins, "Tools the agent may call")).to_be_visible()
    d.point(field(ins, "Tools the agent may call"), "Tools：勾选这个智能体可以调用的工具；Researcher 可用 calculator 和 word_count")
    d.point(field(ins, "Max tool rounds"), "Max tool rounds：一次执行中最多调用几轮工具，防止无限调用")
    d.point(field(ins, "Scripted response"),
            "Deterministic script：只在 deterministic 模式下使用的脚本回答，可以引用模板和 {{tool_results}}", highlight=False)
    d.point(field(ins, "Scripted tool calls"), "Scripted tool calls：脚本模式下模拟的工具调用（JSON），工具本身仍会真实执行")
    d.node_click("needs_revision")
    expect(field(ins, "Operator")).to_be_visible()
    d.point(field(ins, "Left"), "Condition 节点。Left：要判断的值，这里是 Reviewer 的输出")
    d.point(field(ins, "Operator"), "Operator：contains、not_contains、equals、not_equals、greater_than、less_than、is_true")
    d.point(field(ins, "Right"), "Right：比较的对象；这里判断“Reviewer 的输出是否包含 REVISE”")
    d.point(field(ins, "Max true visits"), "Max true visits = 2：循环保护，条件第 3 次为 true 时强制走 false，避免死循环", highlight=True)
    d.node_click("final")
    expect(field(ins, "Template")).to_be_visible()
    d.point(field(ins, "Template"), "Output 节点。Template：用模板拼出最终结果，可以引用输入和任意节点的输出")
    d.ok("every node type's settings explained in the inspector")


def ch_run(d: Demo) -> None:
    page = d.page
    d.chapter("run", "运行、断点与实时状态", "运行对话框的每个选项，以及运行中的画布")
    d.open("/#/workflows/wf_research_review")
    expect(node(page, "reviewer")).to_be_visible()
    d.click(page.get_by_role("button", name="Run", exact=True))
    modal = page.locator(".modal")
    expect(modal).to_be_visible()
    d.point(field(modal, "Topic"), "运行输入：字段来自 Start 节点的定义，可以在这里修改本次运行的值")
    d.choose(field(modal, "Agent provider").locator("select"), "deterministic")
    d.point(field(modal, "Agent provider"),
            "提供者有三个选项：As configured（按工作流设置）、Real model（需要 API key，当前不可选）、Deterministic（脚本回答，离线）。这里选 Deterministic")
    d.point(field(modal, "Breakpoints"), "Breakpoints：勾选的节点在执行前暂停，之后可以在运行页或编辑器里 Resume")
    d.click(modal.locator("label.check", has_text="Final brief"))
    d.point(modal.locator("label.check", has_text="Final brief"), "这次在 Final brief 前设一个断点")
    d.point(page.get_by_role("button", name="Start run"), "Start run：如果有未保存的改动会先保存，然后开始运行")
    d.click(page.get_by_role("button", name="Start run"), pause=0.2)
    expect(page.locator(".run-chip.selected .status-paused")).to_be_visible(timeout=60_000)
    expect(node(page, "final")).to_contain_text("paused")
    expect(node(page, "researcher")).to_contain_text("×3")
    expect(node(page, "reviewer")).to_contain_text("×3")
    d.ok("run (deterministic) paused at the breakpoint before Final brief; Researcher and Reviewer ×3")
    d.point(page.locator(".run-chip.selected"),
            "底部运行条：这个工作流的运行记录。点选一条，画布就叠加显示那次运行的节点状态；open 打开运行详情")
    d.zoom_at(node(page, "reviewer"), 1)
    d.node_point("researcher", "节点上的 ok 是执行结果，×3 表示执行了 3 次", highlight=True)
    d.point(node(page, "needs_revision"),
            "原因：脚本化的 Reviewer 每次都回答 REVISE；条件节点的 Max true visits=2 在第 3 次强制走 false，循环结束")
    d.node_point("final", "Final brief 显示 paused：停在断点前，还没有执行")
    d.point(page.locator(".editor-top").get_by_role("button", name="Resume"),
            "运行暂停时，编辑器顶部会出现 Resume；运行失败时这里会变成 Retry from failure")
    d.node_click("reviewer")
    ins = page.locator(".inspector")
    expect(ins.get_by_text("Selected run · last execution")).to_be_visible()
    d.point(ins.locator("h4", has_text="Selected run"),
            "选中一次运行后，检查器底部显示该节点最近一次执行的状态、输入、输出和子调用")
    d.click(page.locator(".run-chip.selected a"))
    expect(page.locator(".run-head .status-paused")).to_be_visible()
    d.ctx["research_run"] = run_id_from_url(page)
    d.ok("open the paused run from the run strip", run_id=d.ctx["research_run"])


def ch_run_detail(d: Demo) -> None:
    page = d.page
    d.chapter("detail", "运行详情", "顶部信息、能力标签、恢复，以及 Graph / Timeline / Spans")
    run_id = d.ctx.get("research_run")
    if not run_id:
        raise RuntimeError("no run from the previous chapter")
    d.open(f"/#/runs/{run_id}")
    head = page.locator(".run-head")
    expect(head.locator(".status-paused")).to_be_visible()
    d.point(head.locator(".row.muted").first, "面包屑：这次运行属于 Runs 列表中的哪个工作流")
    d.point(head.locator("h1"), "标题和当前状态：paused（暂停）")
    d.point(head.locator(".mono").first, "运行 ID：在 Compare 中可以粘贴它来对比任意两次运行")
    d.point(head.locator(".badge", has_text="deterministic script"),
            "deterministic script：这次运行的智能体使用脚本回答，不是模型输出；真实模型运行显示 real model")
    d.point(head.locator(".badge", has_text="definition v"), "definition v1：运行时保存的定义快照，之后修改工作流不影响这次运行")
    d.point(page.locator(".row", has=page.locator("span", has_text="Capabilities:")).first,
            "Capabilities：这次运行支持的操作——执行、检查点、暂停/恢复、分叉、重放、失败重试；✕ 表示不支持")
    d.point(head.get_by_role("button", name="Resume"), "Resume：从断点处的检查点继续执行")
    d.click(head.get_by_role("button", name="Resume"))
    expect(head.locator(".status-completed")).to_be_visible(timeout=60_000)
    assert d.api(f"/api/runs/{run_id}")["status"] == "completed"
    d.ok("resumed from the run view and completed", run_id=run_id)
    d.say("恢复后运行完成：Final brief 已执行", hold=2.0)

    d.zoom_at(node(page, "reviewer"), 2)
    d.point(page.locator(".graph-box"), "Graph：工作流结构图叠加本次运行的节点状态", highlight=True)
    d.point(page.locator(".graph-box + p, .graph-box ~ p").first,
            "图例：×N 是循环中的执行次数，变淡表示没有执行，inherited 表示分支从父运行继承了该节点的结果")
    d.node_click("planner")
    expect(page.locator(".span-detail")).to_be_visible()
    d.say("点击图中的节点，会跳到 Spans 并选中这个节点最近一次的 span", hold=3.2)

    d.click(page.get_by_role("tab", name="Timeline"))
    expect(page.locator(".tl-row").nth(2)).to_be_visible()
    d.point(page.locator(".tl-row").first, "Timeline：表头给出总时长；每一行是一个 span")
    d.point(page.locator(".tl-row").nth(2),
            "左边：类型标签和名称，按父子关系缩进；中间横条：开始时间和持续时间；右边：耗时。点击行可跳到 Span 详情")
    d.ok("timeline renders")

    d.click(page.get_by_role("tab", name=re.compile("^Spans")))
    split = page.locator(".split")
    d.point(split.locator("input[placeholder^='Filter']"), "Spans：按名称或智能体过滤")
    d.point(split.locator("select").nth(0), "按类型过滤：workflow、agent、llm、tool、condition、output、pause")
    d.point(split.locator("select").nth(1), "按状态过滤：ok、error、running、paused、resumed")
    d.choose(split.locator("select").nth(0), "pause")
    d.point(page.locator(".span-row").first, "例如只看 pause：这条就是刚才在 Final brief 前的断点暂停")
    d.choose(split.locator("select").nth(0), "")
    d.click(page.locator(".span-row", has_text="llm").first)
    detail = page.locator(".span-detail")
    expect(detail.get_by_text("Messages sent to the model")).to_be_visible()
    d.point(detail.locator("dl.kv"),
            "Span 详情：所属节点、Span id、父 span、起止时间、耗时、提供者和模型、对应的检查点、子调用")
    d.point(detail.locator(".json", has_text="Messages sent to the model"),
            "LLM span 会列出发给模型的完整消息：system、user，以及工具结果", highlight=True)
    d.point(detail.locator(".json", has_text="Output").first, "Output：模型的回复（这里是脚本回答）")
    d.click(detail.get_by_role("button", name=re.compile("Attributes")))
    d.point(detail.locator(".json", has_text="Attributes"), "Attributes：原始属性，比如提供者、模型、token 用量")
    d.ok("span explorer: filters, detail fields, model messages and attributes")


def ch_fork(d: Demo) -> None:
    page = d.page
    d.chapter("fork", "检查点、分叉、对比与重放", "从任意检查点修改状态并重跑一条分支")
    run_id = d.ctx.get("research_run")
    if not run_id:
        raise RuntimeError("no run from the previous chapter")
    d.open(f"/#/runs/{run_id}")
    expect(page.locator(".run-head .status-completed")).to_be_visible()
    d.click(page.get_by_role("tab", name="Checkpoints & fork"))
    expect(page.locator(".cp").first).to_be_visible()
    d.point(page.locator(".cp-list"),
            "检查点列表：LangGraph 在每个节点执行后写入一个检查点。每行是：#序号、在哪个节点之后、下一步执行什么", highlight=True)
    d.point(page.locator(".cp .badge", has_text="forkable").first, "forkable：可以从这里分叉；parent 表示继承自父运行；paused 表示停在这里")
    first_review = page.locator(".cp", has_text="after Reviewer").last
    d.click(first_review)
    detail = page.locator(".span-detail")
    expect(detail.get_by_text("Checkpoint after Reviewer")).to_be_visible()
    d.point(detail.locator(".row").first, "选中第一次 Reviewer 之后的检查点（当时的结论是 REVISE）")
    d.point(detail.get_by_role("button", name="Replay from here"), "Replay from here：用完全相同的状态在新分支里继续执行")
    d.point(detail.get_by_role("button", name="Fork with edits"), "Fork with edits：先修改状态，再在新分支里继续执行")
    d.point(detail.locator(".note"), "分叉和重放都会创建新运行（新 thread），原运行和它的历史不会被修改")
    d.point(detail.locator(".json", has_text="Executed path"), "这个检查点的完整状态：已执行的路径、输入，以及每个节点当时的输出")
    d.click(detail.get_by_role("button", name="Fork with edits"))
    dialog = page.get_by_role("dialog")
    d.point(dialog.locator(".note"), "分叉对话框：可以修改输入和各节点的输出；没改的字段保持检查点里的值；条件会按新值重新计算")
    d.type(field(dialog, "nodes.reviewer.output").locator("textarea"), "APPROVED", delay_ms=70)
    d.point(field(dialog, "nodes.reviewer.output"), "把 Reviewer 的输出改成 APPROVED", highlight=True)
    d.point(dialog.get_by_role("button", name="Create branch and run"), "Create branch and run：创建分支并立即运行")
    d.click(dialog.get_by_role("button", name="Create branch and run"))
    page.wait_for_url(lambda url: run_id not in url)
    expect(page.locator(".run-head .status-completed")).to_be_visible(timeout=60_000)
    fork_id = run_id_from_url(page)
    expect(node(page, "planner")).to_contain_text("inherited")
    expect(node(page, "final")).to_contain_text("ok")
    expect(node(page, "needs_revision")).not_to_contain_text("×")
    fork = d.api(f"/api/runs/{fork_id}")
    assert fork["parent_run_id"] == run_id and fork["fork_changes"] == {"nodes.reviewer.output": "APPROVED"}, fork
    d.ctx["fork_id"] = fork_id
    d.ok("fork from the first Reviewer checkpoint with an edited output; lineage persisted", fork_id=fork_id)
    d.point(page.locator(".lineage"), "分支关系：fork of 源运行 · 来自哪个检查点 · 修改了哪个字段", highlight=True)
    d.zoom_at(node(page, "reviewer"), 1)
    d.node_point("researcher", "检查点之前的节点标记为 inherited：直接继承，没有重新执行")
    d.node_point("needs_revision", "条件按新值重新判断：不再包含 REVISE，走 false，只执行一次")
    d.node_point("final", "直接进入 Final brief：这条分支跳过了后面两轮修改")

    d.click(page.get_by_role("tab", name="Compare"))
    expect(page.locator("table").first).to_be_visible(timeout=10_000)
    row = page.locator("tr", has=page.locator("td.mono", has_text=re.compile("^final$")))
    expect(row.locator("td").last).to_have_text("differs")
    d.point(page.locator(".card", has_text="Compare with").locator(".row").first,
            "Compare with：默认选中父运行或子分支，也可以粘贴任意运行 ID 后按回车")
    d.point(page.locator(".json", has_text="Executed path A"), "两次运行实际走过的路径：分支只执行了检查点之后的节点")
    d.point(page.locator("table").first, "逐节点对比输出：inherited 是继承的，differs 是结果不同，same 是相同", highlight=True)
    d.ok("compare: final differs between the branch and its parent")
    d.click(page.get_by_role("tab", name="Run input/output"))
    d.point(page.locator(".card .json").first, "Run input/output：本次运行的输入、最终输出和错误信息")

    d.click(page.locator(".lineage a.mono").first)
    page.wait_for_url(re.compile(re.escape(run_id) + "$"))
    expect(page.locator(".lineage")).to_contain_text("branches")
    d.point(page.locator(".lineage"), "回到源运行：这里列出了它的所有分支，源运行本身没有变化")
    d.click(page.get_by_role("tab", name="Checkpoints & fork"))
    d.click(page.locator(".cp", has_text="after Planner").last)
    d.click(page.locator(".span-detail").get_by_role("button", name="Replay from here"))
    dialog = page.get_by_role("dialog")
    d.point(dialog.locator(".note"), "重放：从这个检查点用相同的状态继续执行。确定性节点会得到相同结果，调用模型的节点会重新调用")
    d.click(dialog.get_by_role("button", name="Replay in new branch"))
    page.wait_for_url(lambda url: run_id not in url)
    expect(page.locator(".run-head .status-completed")).to_be_visible(timeout=60_000)
    replay_id = run_id_from_url(page)
    expect(page.locator(".lineage")).to_contain_text("replay")
    d.click(page.get_by_role("tab", name="Compare"))
    row = page.locator("tr", has=page.locator("td.mono", has_text=re.compile("^final$")))
    expect(row.locator("td").last).to_have_text(re.compile("same|differs"))
    verdict = row.locator("td").last.inner_text().strip()
    d.ok("replay from the Planner checkpoint", replay_id=replay_id, final_verdict=verdict)
    d.point(page.locator("table").first,
            f"重放结果与源运行对比：final 为 {verdict}" + ("——脚本化节点重放后结果相同" if verdict == "same"
                                                         else "——输出中包含每次运行都会变化的内容"), highlight=True)


def ch_build(d: Demo) -> None:
    page = d.page
    d.chapter("build", "新建工作流", "添加节点、实时校验、连线、配置、保存、运行")
    d.open("/#/workflows")
    d.click(page.get_by_role("button", name="New workflow"))
    expect(node(page, "assistant")).to_be_visible()
    workflow_id = page.url.split("/workflows/")[1]
    d.say("新建的工作流自带 Start → Assistant → Answer 三个节点")
    d.type(page.get_by_label("Workflow name"), "Demo: word-count pipeline")
    d.click(page.locator(".palette .palette-item", has=page.locator("strong", has_text=re.compile("^Tool$"))))
    tool = node(page, "tool_1")
    expect(tool).to_be_visible()
    answer = node(page, "answer").bounding_box()
    tb = tool.bounding_box()
    d.glide(tb["x"] + 40, tb["y"] + 15)
    page.mouse.down()
    page.mouse.move(answer["x"] + 20, answer["y"] + 170, steps=20)
    d.mouse = (answer["x"] + 20, answer["y"] + 170)
    page.mouse.up()
    d.wait(0.6)
    expect(page.locator(".editor-top .status-failed")).to_be_visible()
    d.point(page.locator(".editor-top .status-failed"), "实时校验：新的 Tool 节点还没有出边，顶部显示 1 issue(s)")
    d.point(page.locator(".inspector .error-box"), "检查器里列出具体问题；问题修好之前 Run 按钮是禁用的")
    ins = page.locator(".inspector")
    d.choose(field(ins, "Tool").locator("select"), "word_count")
    d.point(field(ins, "Tool"), "Tool：选择 word_count；下面的参数会随所选工具变化")
    d.type(field(ins, "Argument: text").locator("input"), "{{nodes.assistant.output}}", delay_ms=18)
    d.point(field(ins, "Argument: text"), "参数可以用模板：这里统计 Assistant 输出的词数")
    d.drag(page.locator("[data-testid=node-assistant] .react-flow__handle.source"),
           page.locator("[data-testid=node-tool_1] .react-flow__handle.target"))
    d.say("从 Assistant 右侧拖线到 Tool；同一个出口只保留一条连线，原来通往 Answer 的线被替换", hold=3.6)
    d.drag(page.locator("[data-testid=node-tool_1] .react-flow__handle.source"),
           page.locator("[data-testid=node-answer] .react-flow__handle.target"))
    d.node_click("answer")
    d.type(field(ins, "Template").locator("textarea"), "Words: {{nodes.tool_1.output.words}} | {{nodes.assistant.output}}", delay_ms=14)
    expect(page.get_by_text(re.compile(r"\d+ issue\(s\)"))).to_have_count(0, timeout=5000)
    d.point(page.locator(".editor-top span.muted").first, "连好以后问题消失，状态显示 unsaved changes")
    d.click(page.get_by_role("button", name="Save"))
    expect(page.get_by_text("· saved")).to_be_visible()
    saved = d.api(f"/api/workflows/{workflow_id}")
    edges = sorted((e["source"], e["target"]) for e in saved["definition"]["edges"])
    assert edges == sorted([("start", "assistant"), ("assistant", "tool_1"), ("tool_1", "answer")]), edges
    d.ok("workflow built in the editor and saved (edges verified via API)", workflow_id=workflow_id)
    d.point(page.locator(".editor-top span.muted").first, "保存后版本变为 v2（已通过接口核对：3 条连线）")
    d.click(page.get_by_role("button", name="Run", exact=True))
    d.choose(page.locator(".modal select"), "deterministic")
    d.click(page.get_by_role("button", name="Start run"))
    expect(page.locator(".run-chip.selected .status-completed")).to_be_visible(timeout=30_000)
    d.click(node(page, "tool_1"))
    expect(ins.get_by_text("Selected run · last execution")).to_be_visible()
    expect(node(page, "tool_1")).not_to_contain_text("×")
    d.point(ins.locator(".json", has_text="Output").first, "运行完成：Tool 节点的真实输出就是统计出的词数", highlight=True)
    d.ok("new workflow ran; the Tool node shows one execution and its output")


def ch_failure(d: Demo) -> None:
    page = d.page
    d.chapter("failure", "条件分支、失败与重试", "Calculator router：正常分支、出错节点、堆栈、重试")
    d.open("/#/workflows/wf_calculator_router")
    expect(node(page, "calc")).to_be_visible()
    d.point(page.locator(".canvas"), "Calculator router 不用大模型：Calculator 工具 → 条件“结果 > 100？” → 两个输出")
    d.click(page.get_by_role("button", name="Run", exact=True))
    d.point(page.locator(".modal textarea").first, "先用默认输入 12 * (7 + 5)")
    d.click(page.get_by_role("button", name="Start run"))
    expect(page.locator(".run-chip.selected .status-completed")).to_be_visible(timeout=30_000)
    expect(node(page, "large")).to_contain_text("ok")
    d.node_point("large", "144 > 100，条件为 true，走 Large result")
    d.node_point("small", "Small result 变淡：这次没有执行")
    d.ok("condition routing: 12*(7+5) took the 'Large result' branch")
    d.click(page.get_by_role("button", name="Run", exact=True))
    d.type(page.locator(".modal textarea").first, "1/0", delay_ms=90)
    d.say("再故意输入 1/0", hold=1.4)
    d.click(page.get_by_role("button", name="Start run"))
    expect(page.locator(".run-chip.selected .status-failed")).to_be_visible(timeout=30_000)
    expect(node(page, "calc").locator(".status-error")).to_be_visible()
    d.node_point("calc", "出错的节点在画布上直接标红", highlight=True)
    d.point(page.locator(".editor-top").get_by_role("button", name="Retry from failure"), "编辑器顶部出现 Retry from failure")
    d.click(page.locator(".run-chip.selected a"))
    run_id = run_id_from_url(page)
    box = page.locator(".error-box").first
    expect(box).to_contain_text("division by zero")
    d.point(box, "错误框：异常类型、出错节点、错误信息，以及重试会做什么")
    d.click(box.get_by_role("button", name="Inspect failing span"))
    detail = page.locator(".span-detail")
    expect(detail.locator(".error-box")).to_contain_text("division by zero")
    if detail.locator("summary", has_text="Stack trace").count():
        d.click(detail.locator("summary", has_text="Stack trace"))
        d.point(detail.locator(".error-box"), "Inspect failing span：跳到出错的 span，并可展开完整的 Python 堆栈", highlight=True)
    else:
        d.point(detail.locator(".error-box"), "Inspect failing span：跳到出错的 span。工具错误（ToolError）显示类型和消息；"
                "未预期的异常还会附带完整堆栈", highlight=True)
    d.point(detail.locator(".json", has_text="Input").first, "这个 span 的输入就是导致错误的表达式 1/0")
    d.ok("failure surfaced: highlighted node, error box, failing span", run_id=run_id)
    retry = page.get_by_role("button", name="Retry failed node")
    d.point(retry, "Retry failed node：从失败节点之前的检查点重新执行这个节点")
    errors_before = sum(s["status"] == "error" for s in d.api(f"/api/runs/{run_id}/spans"))
    d.click(retry)
    deadline = time.time() + 30
    while time.time() < deadline:
        spans = d.api(f"/api/runs/{run_id}/spans")
        status = d.api(f"/api/runs/{run_id}")["status"]
        if status in ("failed", "completed") and sum(s["status"] == "error" for s in spans) > errors_before or status == "completed":
            break
        d.wait(0.5)
    d.wait(1.0)
    d.ok("retry executed", run_id=run_id, status_after_retry=status)
    d.say("重试后状态：" + status + ("。输入仍是 1/0，所以再次失败——这是真实结果；改正输入或工具后重试才会成功"
                                      if status == "failed" else ""), hold=5.0)


def ch_connected(d: Demo, agent_port: int, m1_port: int) -> None:
    page = d.page
    d.chapter("connected", "接入外部 LangGraph 智能体", "连接对话框的每个选项 · 由 Studio 启动 · SDK 接入")
    d.open("/#/connected")
    expect(page.get_by_text("No connected executors")).to_be_visible()
    d.point(page.locator(".page-head p"),
            "Connected agents：调试在 Studio 之外、自己进程里运行的 LangGraph 智能体；Studio 读取它的检查点，并把暂停、恢复、分叉、重放转发给它")
    d.click(page.get_by_role("button", name="Connect executor"))
    dialog = page.get_by_role("dialog")
    d.point(dialog.locator(".note").first, "执行器保留自己的进程、环境、凭据和检查点数据库；Studio 只调用它的控制接口，不打开它的文件")
    d.point(field(dialog, "Agent type"),
            "Agent type：本项目原有的 M1 执行系统，或者任何用 studio-executor SDK 包装的 LangGraph 智能体")
    d.point(field(dialog, "Name"), "Name：在 Studio 里显示的名字")
    d.choose(field(dialog, "How is the executor started").locator("select"), "attach")
    d.point(field(dialog, "Executor URL"), "如果选“我自己启动”：填写它的地址（只允许本机地址）")
    d.point(field(dialog, "Which model"), "并说明那个进程用的是真实模型、脚本模型还是未知，用于界面标注")
    d.choose(field(dialog, "How is the executor started").locator("select"), "launch")
    d.point(field(dialog, "How is the executor started"), "这次选“由 Studio 启动”：作为独立进程运行，使用独立的数据目录")
    d.type(field(dialog, "Port for the executor").locator("input"), str(m1_port), delay_ms=60)
    d.point(field(dialog, "Port for the executor"), "Port：执行器监听的本机端口")
    d.point(field(dialog, "Model"), "Model：Deterministic（离线规则网关）或 Real model（使用 Studio 环境里的 OPENAI_API_KEY）")
    d.click(dialog.get_by_role("button", name="Add connector"))
    card = page.locator(".card", has_text="M1 LangGraph executor").first
    expect(card.get_by_text("offline")).to_be_visible()
    d.point(card.locator(".row").first, "新连接器：offline（还没启动）、started by the Studio（由 Studio 启动）；右侧可以删除，已镜像的运行会保留")
    d.point(card.get_by_role("button", name="Start executor"), "Start executor：启动执行器进程")
    d.click(card.get_by_role("button", name="Start executor"))
    expect(card.get_by_text("connected", exact=True)).to_be_visible(timeout=90_000)
    d.ok("M1 executor started by the Studio as a separate process")
    d.point(card.locator("p.mono"), "地址、进程号、进程状态，以及已有多少次运行镜像到 Studio")
    d.point(card.locator(".row", has_text="Graph (").first, "Graph：节点列表来自执行器自己的 LangGraph 图，不是 Studio 猜的", highlight=True)
    d.point(card.locator(".row", has_text="Capabilities:").first,
            "Capabilities：执行器支持的操作；fork may edit：分叉时允许修改的状态字段（plan、worker_instruction）")
    d.point(card.locator("ul"), "执行器自己声明的说明，例如暂停只在 pause_gate 生效")
    d.point(field(card, "Task"), "输入字段也由执行器声明：Task 和可选的 Allowed files")
    d.type(field(card, "Task").locator("textarea"), M1_TASK, delay_ms=22)
    d.point(card.get_by_role("button", name="Browse the executor"), "Browse the executor's own runs：浏览执行器自己的运行记录，可以导入到 Studio")
    d.click(card.get_by_role("button", name="Run on executor"))
    page.wait_for_url(re.compile(r"#/runs/run_"))
    m1_run = run_id_from_url(page)
    expect(page.locator("h1 .status-completed")).to_be_visible(timeout=90_000)
    expect(node(page, "coder")).to_contain_text("ok")
    expect(node(page, "web_surfer")).to_contain_text("not run")
    d.ctx["m1_run"] = m1_run
    d.ok("run executed by the M1 executor and mirrored into the Studio", run_id=m1_run)
    d.point(page.locator(".run-head .badge", has_text="connected executor"),
            "connected executor：运行发生在执行器进程里；旁边是执行器自己的运行 ID")
    d.node_point("coder", "离线模式下，任务中提到 code 就交给 Coder 执行")
    d.node_point("web_surfer", "WebSurfer 显示 not run：这次没有走到这个节点")
    d.click(page.get_by_role("tab", name=re.compile("^Spans")))
    expect(page.locator(".span-row", has_text="m1.run").first).to_be_visible()
    d.point(page.locator(".span-row", has_text="m1.run").first,
            "m1.* span 是执行器自己通过 OTLP 上报的，按运行 ID 自动挂到这次运行下")
    d.ok("executor's own OTLP spans correlated to the run")

    d.open("/#/connected")
    d.click(page.get_by_role("button", name="Connect executor"))
    dialog = page.get_by_role("dialog")
    d.choose(field(dialog, "Agent type").locator("select"), "studio_executor")
    d.point(field(dialog, "Agent type"), "第二个连接器：一个独立编写的 LangGraph 客服智能体，用 studio-executor SDK 包装")
    d.point(dialog.locator(".note").nth(1), "智能体由你自己启动，启动时会打印地址和 token；包装自己的图只需要几行代码")
    d.type(field(dialog, "Name").locator("input"), "Support answer agent")
    d.type(field(dialog, "Agent URL").locator("input"), f"http://127.0.0.1:{agent_port}", delay_ms=20)
    d.type(field(dialog, "Token").locator("input"), TOKEN, delay_ms=8)
    d.point(field(dialog, "Token"), "Token：每个请求都要带上；它保存在 Studio 数据库里，之后不再显示")
    d.click(dialog.get_by_role("button", name="Add connector"))
    card = page.locator(".card", has_text="Support answer agent").first
    expect(card.get_by_text("connected", exact=True)).to_be_visible(timeout=30_000)
    d.point(card.locator(".row").first, "连接成功：SDK agent，deterministic script（智能体自己报告使用的模型类型）")
    d.point(card.locator(".row", has_text="Capabilities:").first, "可分叉修改的字段由智能体声明：category、facts、draft、review")
    d.type(field(card, "Customer question").locator("textarea"), QUESTION, delay_ms=22)
    d.click(card.get_by_role("button", name="Run on executor"))
    page.wait_for_url(re.compile(r"#/runs/run_"))
    sdk_run = run_id_from_url(page)
    expect(page.locator("h1 .status-completed")).to_be_visible(timeout=60_000)
    expect(node(page, "draft")).to_contain_text("×2")
    d.ok("SDK agent run mirrored (draft/review loop ×2)", run_id=sdk_run)
    d.zoom_at(node(page, "draft"), 1)
    d.point(page.locator(".graph-box"), "Studio 从智能体的检查点重建图：draft 和 review 循环了 2 次；虚线是条件路由", highlight=True)
    d.click(page.get_by_role("tab", name="Checkpoints & fork"))
    d.click(page.locator(".cp", has_text="after review").filter(has_text="→ draft").last)
    detail = page.locator(".span-detail")
    expect(detail.get_by_text("review (written at this step · editable in a fork)")).to_be_visible()
    d.point(detail.locator(".note"), "检查点直接从智能体自己的存储读取；分叉和重放由智能体在原 thread 中创建新分支")
    d.point(detail.locator(".json", has_text="editable in a fork").first,
            "这里只显示智能体声明为可编辑的字段；完整状态在下方 Full executor state 中")
    d.click(detail.get_by_role("button", name="Fork with edits"))
    dialog = page.get_by_role("dialog")
    d.type(field(dialog, "review").locator("textarea"), "APPROVED", delay_ms=70)
    d.point(field(dialog, "review"), "把第一次审核的结论从 REVISE 改成 APPROVED；智能体会再校验一次", highlight=True)
    d.click(dialog.get_by_role("button", name="Create branch and run"))
    page.wait_for_url(lambda url: sdk_run not in url)
    expect(page.locator("h1 .status-completed")).to_be_visible(timeout=60_000)
    expect(node(page, "classify")).to_contain_text("inherited")
    expect(node(page, "finalize")).to_contain_text("ok")
    d.ok("fork executed by the agent: revision skipped, earlier nodes inherited", fork_id=run_id_from_url(page))
    d.click(page.get_by_role("tab", name="Compare"))
    row = page.locator("tr", has=page.locator("td.mono", has_text=re.compile("^finalize$")))
    expect(row.locator("td").last).to_have_text("differs")
    d.point(page.locator("table").first, "Compare：classify、lookup 继承；跳过第二轮修改后，finalize 的最终回答不同", highlight=True)
    d.ok("compare: finalize differs between source run and branch")

    d.open("/#/connected")
    runs_table = page.locator("table").last
    expect(runs_table).to_contain_text("fork of")
    d.point(runs_table, "Runs on connected executors：所有接入智能体的运行，Lineage 列显示分支关系")
    card = page.locator(".card", has_text="M1 LangGraph executor").first
    d.point(card.get_by_role("button", name="Stop executor"), "Stop executor：停止由 Studio 启动的执行器进程")
    d.click(card.get_by_role("button", name="Stop executor"))
    expect(card.get_by_text("offline")).to_be_visible(timeout=30_000)
    d.open(f"/#/runs/{d.ctx['m1_run']}")
    note = page.get_by_text("The executor is not reachable right now")
    expect(note).to_be_visible()
    d.point(note, "执行器离线后，Studio 仍显示上次同步的历史；此时控制操作会返回错误")
    d.ok("executor stopped: mirrored history still readable")


def ch_external(d: Demo) -> None:
    page = d.page
    d.chapter("external", "Integrations 与外部轨迹", "OTLP 只读观测 · 成功与失败两种轨迹")
    d.open("/#/integrations")
    card = page.locator(".card", has_text="External research agent").first
    expect(card).to_be_visible()
    d.point(page.locator(".page-head p"), "Integrations：这里的智能体只上报轨迹（只读）；能暴露检查点的 LangGraph 智能体请用 Connected agents")
    d.point(card.locator(".row").first, "示例外部智能体：executable 表示可以从这里启动；trace-only control 表示 Studio 只能看，不能控制")
    d.point(card.locator("p.mono"), "它是独立的 Python 程序（Analyst → Critic），不是 LangGraph，只用 OpenTelemetry 埋点")
    d.point(field(card, "Topic"), "启动参数。Topic：研究主题")
    d.point(field(card, "Mode"), "Mode：deterministic（离线脚本）或 openai（真实模型）")
    d.point(field(card, "Simulate tool failure"), "Simulate tool failure：设为 true 时让 word_count 工具故意报错，用来演示错误捕获")
    d.click(card.get_by_role("button", name="Launch agent process"))
    link = card.get_by_role("link", name="Open trace →")
    expect(link).to_be_visible(timeout=30_000)
    d.point(card.locator(".row").last, "启动后显示进程状态、pid 和退出码；轨迹到达后出现 Open trace")
    d.click(link)
    expect(page.get_by_text("trace-only (read-only)")).to_be_visible()
    expect(page.get_by_role("tab", name="Checkpoints & fork")).to_be_disabled()
    d.ok("external OTLP trace ingested and shown read-only")
    d.point(page.locator(".run-head .badge", has_text="trace-only"), "trace-only (read-only)：Studio 和这个进程之间没有控制通道")
    d.point(page.locator(".row", has=page.locator("span", has_text="Capabilities:")).first, "所以所有能力都是 ✕：不能暂停、分叉或重放")
    d.point(page.get_by_role("tab", name="Checkpoints & fork"), "Checkpoints & fork 标签不可用：外部轨迹没有检查点")
    d.point(page.locator(".graph-box"), "图是按 span 的父子关系画出的调用树，不是工作流结构；任何框架只要发 span 都能得到", highlight=True)
    d.click(page.get_by_role("tab", name=re.compile("^Spans")))
    d.click(page.locator(".span-row", has_text="analyst.llm").first)
    d.point(page.locator(".span-detail dl.kv"), "span 的类型和内容来自 OpenInference 属性：span.kind、input.value、output.value 等")

    d.open("/#/integrations")
    card = page.locator(".card", has_text="External research agent").first
    d.type(field(card, "Simulate tool failure").locator("input"), "true", delay_ms=80)
    d.point(field(card, "Simulate tool failure"), "再启动一次，这次设为 true")
    d.click(card.get_by_role("button", name="Launch agent process"))
    link = card.get_by_role("link", name="Open trace →")
    expect(link).to_be_visible(timeout=30_000)
    d.click(link)
    expect(page.locator("h1 .status-failed")).to_be_visible(timeout=20_000)
    spans = d.api(f"/api/runs/{run_id_from_url(page)}/spans")
    tool_failed = any(s["name"] == "word_count" and s["status"] == "error" for s in spans)
    critic_ran = any("critic" in s["name"].lower() for s in spans)
    assert tool_failed, [s["name"] for s in spans]
    d.ok("failing external trace: tool span error captured", critic_spans=critic_ran)
    d.point(page.locator(".error-box").first, "失败轨迹：span 的错误状态和 exception 事件被识别为失败，并保留堆栈", highlight=True)
    d.point(page.locator(".graph-box"), "word_count 标红" + ("；工具抛出异常后程序退出，Critic 没有执行，所以图里没有它" if not critic_ran
                                                          else "；Critic 仍然执行了"))
    d.open("/#/integrations")
    otlp = page.locator(".card", has_text="OTLP/HTTP traces endpoint").first
    d.point(field(otlp, "OTLP/HTTP traces endpoint"), "OTLP 接收地址：任何框架把 OpenTelemetry 导出器指向这里即可")
    d.point(otlp.locator(".note"), "Python 示例代码，或设置环境变量 OTEL_EXPORTER_OTLP_TRACES_ENDPOINT；下方列出能识别的属性")
    d.nav("External traces")
    expect(page.locator("table")).to_contain_text("trace-only")
    d.point(page.locator("table"), "External traces：所有外部轨迹，标注 trace-only；上方可以按名称搜索、按状态筛选")
    d.ok("external traces list shows both traces")


def ch_runs(d: Demo) -> None:
    page = d.page
    d.chapter("runs", "Runs 运行记录", "Studio 工作流的全部运行与分支")
    d.open("/#/workflows")
    d.nav("Runs")
    expect(page.locator("table")).to_be_visible()
    d.point(page.locator(".page-head p"),
            "Runs 只列出 Studio 工作流的运行，包括分叉和重放；外部智能体的运行在 Connected agents，外部轨迹在 External traces")
    d.point(page.locator("input[placeholder^='Search']"), "按工作流名称或运行 ID 搜索")
    d.choose(page.locator(".page select"), "failed")
    d.point(page.locator(".page select"), "按状态筛选：例如只看 failed，就是刚才 1/0 的那次运行")
    d.choose(page.locator(".page select"), "")
    head = page.locator("thead")
    d.point(head.locator("th", has_text="Kind"), "Kind：智能体用的提供者——deterministic script、real model；没有 Agent 节点的工作流显示 no-llm。分支会标出 fork of / replay of")
    d.point(head.locator("th", has_text="Spans"), "Duration、Spans、Error：耗时、span 数量，以及失败时的出错节点和原因")
    fork_row = page.locator("tbody tr", has_text="fork of")
    if fork_row.count():
        d.point(fork_row.first, "带 fork of 标签的是分支运行；点击任意一行打开运行详情", highlight=True)
    else:
        d.point(page.locator("tbody tr").first, "点击任意一行打开运行详情", highlight=True)
    runs = d.api("/api/runs?origin=studio")
    d.ok("runs list shows Studio workflow runs with lineage", runs=len(runs))


# ======================================================================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=None, help="output directory (default data/demo/<timestamp>)")
    parser.add_argument("--pace", type=float, default=float(os.environ.get("DEMO_PACE", "1.0")),
                        help="multiply every pause and typing delay (e.g. 1.3 = slower)")
    parser.add_argument("--headed", action="store_true", help="show the browser window while recording")
    parser.add_argument("--only", default="", help="comma-separated chapter keys to record (for debugging)")
    args = parser.parse_args()

    out = args.out or ROOT / "data" / "demo" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="studio-demo-"))
    port, agent_port, m1_port = free_port(), free_port(), free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONUTF8": "1", "STUDIO_EXECUTOR_TOKEN": TOKEN,
           "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")]))}
    env.pop("OPENAI_API_KEY", None)  # the demo is offline by design; the UI then labels runs deterministic
    log = open(out / "processes.log", "wb")
    agent = subprocess.Popen([sys.executable, str(ROOT / "examples" / "langgraph_agent" / "support_agent.py"),
                              "--port", str(agent_port), "--data-dir", str(work / "agent")],
                             cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    server = subprocess.Popen([sys.executable, "-m", "m1_local.cli", "studio", "--port", str(port),
                               "--data-dir", str(work / "studio")], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    manifest: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "studio": base,
        "agent": f"examples/langgraph_agent/support_agent.py :{agent_port} (deterministic)",
        "providers": "deterministic everywhere (no model calls); OPENAI_API_KEY removed from the child environment",
        "platform": f"{platform.system()} {platform.release()} / Python {platform.python_version()}",
        "viewport": {**VIEWPORT, "device_scale_factor": SCALE}, "pace": args.pace,
    }
    try:
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        manifest["git"] = git.stdout.strip() + (" (uncommitted changes)" if dirty else "")
    except OSError:
        manifest["git"] = "unknown"
    status = 1
    demo = None
    try:
        wait_http(base + "/api/health")
        wait_http(f"http://127.0.0.1:{agent_port}/studio/v1/health", headers={"x-studio-executor-token": TOKEN})
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            manifest["chromium"] = browser.version
            context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE, locale="en-US")
            context.add_init_script(OVERLAY_JS)
            page = context.new_page()
            page.set_default_timeout(15_000)
            errors = {"console": [], "page": [], "http_errors": []}
            page.on("console", lambda m: errors["console"].append(m.text[:300]) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors["page"].append(str(e)[:300]))
            page.on("response", lambda r: errors["http_errors"].append(f"{r.status} {r.request.method} {r.url}")
                    if r.status >= 400 else None)
            page.goto(base + "/#/workflows")
            page.wait_for_load_state("networkidle")
            demo = Demo(page, base, args.pace)
            cast = Screencast(context, page, out / "frames")
            cast.start()
            demo.wait(0.6)
            chapters = [("tour", ch_tour), ("settings", ch_settings), ("editor", ch_editor), ("run", ch_run),
                        ("detail", ch_run_detail), ("fork", ch_fork), ("build", ch_build), ("failure", ch_failure),
                        ("connected", lambda d: ch_connected(d, agent_port, m1_port)), ("external", ch_external),
                        ("runs", ch_runs)]
            only = {k for k in args.only.split(",") if k}
            for key, fn in chapters:
                if only and key not in only:
                    continue
                try:
                    fn(demo)
                except Exception as exc:  # noqa: BLE001 - record, keep the failure on screen, continue
                    if not demo.chapters:
                        raise
                    try:
                        page.evaluate("() => window.__demoSpot && window.__demoSpot(null)")
                    except Exception:  # noqa: BLE001
                        pass
                    demo.fail(f"chapter '{demo.chapters[-1]['title']}' stopped", exc)
                    try:
                        page.screenshot(path=str(out / f"failure-{demo.chapters[-1]['key']}.png"))
                    except Exception:  # noqa: BLE001
                        pass
                    demo.wait(4.0)
            demo.wait(1.0)
            cast.stop()
            manifest["recording"] = {"frames": cast.frames, "start": cast.started, "end": cast.stopped,
                                     "frame_size": [int(VIEWPORT["width"] * SCALE), int(VIEWPORT["height"] * SCALE)]}
            manifest["browser_errors"] = errors
            browser.close()
        failed = [s for s in demo.steps if s["status"] != "PASS"]
        manifest["result"] = "PASS" if not failed else "FAIL"
        status = 0 if not failed else 2
    except Exception as exc:  # noqa: BLE001
        manifest["result"] = "ERROR"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["traceback"] = traceback.format_exc()
        print(manifest["traceback"], flush=True)
    finally:
        if demo is not None:
            manifest.update(chapters=demo.chapters, captions=demo.captions, steps=demo.steps)
        for proc in (server, agent):
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        steps = manifest.get("steps", [])
        print(f"\nRESULT {manifest.get('result')} · {sum(s['status'] == 'PASS' for s in steps)}/{len(steps)} steps · "
              f"{len(manifest.get('recording', {}).get('frames', []))} frames · {out}", flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())

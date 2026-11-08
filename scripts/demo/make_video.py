"""Turn a recording made by record_demo.py into the demo video, a short preview and a coverage report.

    python scripts/demo/make_video.py data/demo/<timestamp> [--out docs/demo]

Produces (in --out):
  agent-studio-demo.mp4      full demo, 1920x1080 30 fps H.264: title card, chapter cards with cross-fades,
                             burned-in captions (failures in red), end card with the step results
  agent-studio-preview.gif   ~30 s README preview (highlights, 960 px, 10 fps)
  agent-studio-preview.mp4   same cuts as an MP4 (1280 px)
  COVERAGE.md                feature coverage list generated from the recorded step results
  demo-manifest.json         copy of the recording manifest (frame list omitted)

Nothing is re-timed or sped up: every chapter segment is the real-time recording between two chapter marks.
Requires ffmpeg/ffprobe on PATH (with libass and libx264) and Playwright Chromium (renders the cards).
"""
from __future__ import annotations

import argparse
import html
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
W, H, FPS = 1920, 1080, 30
XFADE = 0.5            # cross-fade length between clips (s)
TITLE_S, CARD_S, END_S = 5.0, 2.4, 7.0
MAX_CUTS = 12          # highlight clips in the README preview
SIDEBAR_W = 342        # the Studio sidebar at 1.5x scale; captions are centred on the content area to its right
FONT = "Microsoft YaHei" if platform.system() == "Windows" else "Noto Sans CJK SC"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit(f"command failed: {' '.join(cmd[:6])} ...")


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


# ---------------------------------------------------------------------------------------------------------- cards ----
CARD_CSS = """
*{margin:0;box-sizing:border-box} body{width:1920px;height:1080px;overflow:hidden;
font-family:'Noto Sans CJK SC','Microsoft YaHei','PingFang SC',Inter,system-ui,sans-serif;color:#eef0ff;
background:radial-gradient(1200px 700px at 78% 18%,#3b2f8f 0%,rgba(59,47,143,0) 60%),
radial-gradient(900px 600px at 10% 100%,#1d3b6b 0%,rgba(29,59,107,0) 60%),#0f1020}
.wrap{position:absolute;inset:0;padding:120px 150px;display:flex;flex-direction:column;justify-content:center}
.logo{width:84px;height:84px;border-radius:20px;background:#6c5ce7;display:flex;align-items:center;justify-content:center;margin-bottom:36px}
.k{font-size:28px;letter-spacing:.18em;color:#a9a3ff;text-transform:uppercase;margin-bottom:18px}
h1{font-size:96px;font-weight:800;letter-spacing:-.01em;line-height:1.08}
h2{font-size:44px;font-weight:500;color:#c9cbe8;margin-top:26px;line-height:1.35}
.num{font-size:200px;font-weight:800;color:rgba(169,163,255,.22);position:absolute;right:140px;bottom:70px;line-height:1}
.chips{display:flex;gap:16px;flex-wrap:wrap;margin-top:54px}
.chip{font-size:26px;padding:10px 22px;border-radius:999px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.16)}
.note{position:absolute;left:150px;right:150px;bottom:70px;font-size:24px;color:#9ea2c6;line-height:1.5}
table{border-collapse:collapse;margin-top:40px;font-size:27px} td{padding:8px 26px 8px 0;color:#d7d9f2}
td.ok{color:#5be3a1;font-weight:700} td.bad{color:#ff7b7b;font-weight:700}
.steps{columns:2;column-gap:60px;margin-top:30px;font-size:19px;color:#d7d9f2}
.row{display:flex;gap:14px;break-inside:avoid;padding:4px 0;line-height:1.28} .row b{width:22px;flex:none}
.ok{color:#5be3a1} .bad{color:#ff7b7b}
code{font-family:'JetBrains Mono',Consolas,monospace;font-size:24px;background:rgba(255,255,255,.08);padding:4px 10px;border-radius:8px}
"""
LOGO = ('<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round">'
        '<circle cx="6" cy="6" r="2.4"/><circle cx="18" cy="6" r="2.4"/><circle cx="12" cy="18" r="2.4"/>'
        '<path d="M7.5 8l3.3 7.6M16.5 8l-3.3 7.6"/></svg>')


def card_html(body: str) -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CARD_CSS}</style></head><body>{body}</body></html>"


def render_cards(cards: dict[str, str], out: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H})
        for name, body in cards.items():
            page.set_content(card_html(body))
            page.wait_for_timeout(150)
            page.screenshot(path=str(out / f"{name}.png"))
        browser.close()


# ------------------------------------------------------------------------------------------------------- captions ----
def ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def caption_spans(captions: list[dict], length: float) -> list[tuple[float, float, dict]]:
    """When each caption is shown, relative to its chapter segment (shared by the burned-in ASS and the SRT file)."""
    spans = []
    for i, cap in enumerate(captions):
        start = cap["rel"]
        end = captions[i + 1]["rel"] if i + 1 < len(captions) else length
        if cap.get("dur") and cap["kind"] != "error":
            end = min(end, start + cap["dur"] + 0.8)   # do not leave a caption up over later, unrelated actions
        end = min(length, max(end, start + 1.5))
        spans.append((start, end, cap))
    return spans


def srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def write_srt(path: Path, seg_info: list[dict], clip_starts: list[float], clip_lengths: list[float]) -> int:
    """Subtitles for the final video: the chapter title over each chapter card, then every caption at its final time.
    Clips are [title, (card, segment)*, end]; card k is clip 1+2k and its segment is clip 2+2k."""
    entries = []
    for k, info in enumerate(seg_info):
        card, seg = 1 + 2 * k, 2 + 2 * k
        c = info["chapter"]
        entries.append((clip_starts[card] + XFADE, clip_starts[card] + clip_lengths[card] - XFADE,
                        f"第 {k + 1} 章 · {c['title']}\n{c['subtitle']}"))
        for start, end, cap in caption_spans(info["caps"], info["length"]):
            text = ("⚠ " + cap["text"]) if cap["kind"] == "error" else cap["text"]
            entries.append((clip_starts[seg] + start, clip_starts[seg] + end - 0.05, text))
    blocks = [f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n" for n, (a, b, text) in enumerate(entries, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8-sig")   # BOM: Windows players then read UTF-8 correctly
    return len(entries)


def segment_ass(path: Path, length: float, index: int, total: int, chapter: dict, captions: list[dict]) -> None:
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding",
        f"Style: Cap,{FONT},38,&H00FFFFFF,&H00FFFFFF,&H28140F14,&H00000000,0,0,0,0,100,100,0,0,3,16,0,2,{SIDEBAR_W + 70},70,36,1",
        f"Style: Err,{FONT},36,&H00FFFFFF,&H00FFFFFF,&H202020C8,&H00000000,1,0,0,0,100,100,0,0,3,16,0,2,{SIDEBAR_W + 70},70,36,1",
        f"Style: Tag,{FONT},26,&H00FFC4B9,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        f"Style: Sub,{FONT},22,&H00C6A29E,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        # chapter tag in the empty part of the dark sidebar (never covers controls)
        f"Dialogue: 1,{ass_time(0)},{ass_time(length)},Tag,,0,0,0,,{{\\pos(28,800)}}{index:02d} / {total:02d}",
        f"Dialogue: 1,{ass_time(0)},{ass_time(length)},Tag,,0,0,0,,{{\\pos(28,836)\\fs30\\c&HFFFFFF&}}{ass_escape(chapter['title'])}",
        f"Dialogue: 1,{ass_time(0)},{ass_time(length)},Sub,,0,0,0,,{{\\pos(28,880)}}实时录屏 · 未加速",
    ]
    for start, end, cap in caption_spans(captions, length):
        style = "Err" if cap["kind"] == "error" else "Cap"
        prefix = "⚠ " if cap["kind"] == "error" else ""
        lines.append(f"Dialogue: 2,{ass_time(start)},{ass_time(end - 0.05)},{style},,0,0,0,,"
                     "{" + ("\\an8" if cap.get("pos") == "top" else "") + "\\fad(180,120)}" + prefix + ass_escape(cap["text"]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------------------------------------- main ----
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", type=Path, help="directory written by record_demo.py")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "demo")
    parser.add_argument("--keep", action="store_true", help="keep intermediate clips")
    parser.add_argument("--srt-only", action="store_true",
                        help="only (re)write the .srt from an existing build made with --keep (no encoding)")
    args = parser.parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found on PATH")

    rec = args.recording.resolve()
    m = json.loads((rec / "manifest.json").read_text(encoding="utf-8"))
    if "recording" not in m:
        raise SystemExit(f"the recording did not complete: {m.get('error')}")
    frames, chapters, steps = m["recording"]["frames"], m["chapters"], m["steps"]
    work = rec / "build"
    work.mkdir(exist_ok=True)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    t0, t_end = frames[0]["t"], m["recording"]["end"]
    if args.srt_only:
        names = ["title"] + [n for i in range(1, len(chapters) + 1) for n in (f"ch{i:02d}", f"seg{i:02d}")] + ["end"]
        lengths = [probe_duration(work / f"{n}.mp4") for n in names]
        starts = [0.0]
        for L in lengths[:-1]:
            starts.append(starts[-1] + L - XFADE)
        raw_len = probe_duration(work / "raw.mp4")
        info = []
        for i, c in enumerate(chapters, 1):
            seg_len = ((chapters[i]["t"] - t0) if i < len(chapters) else raw_len) - (c["t"] - t0)
            info.append({"chapter": c, "length": seg_len,
                         "caps": [{**cap, "rel": cap["t"] - c["t"]} for cap in m["captions"] if cap["chapter"] == c["key"]]})
        n = write_srt(out / "agent-studio-demo.srt", info, starts, lengths)
        print(f"{out / 'agent-studio-demo.srt'} ({n} entries)")
        return 0

    # 1) raw real-time video from the screencast frames (variable frame timing -> constant 30 fps)
    lst = ["ffconcat version 1.0"]
    for i, f in enumerate(frames):
        nxt = frames[i + 1]["t"] if i + 1 < len(frames) else t_end
        lst += [f"file '{(rec / 'frames' / f['file']).as_posix()}'", f"duration {max(nxt - f['t'], 0.001):.4f}"]
    lst.append(f"file '{(rec / 'frames' / frames[-1]['file']).as_posix()}'")
    (work / "frames.ffconcat").write_text("\n".join(lst) + "\n", encoding="utf-8")
    print("1/5 raw video", flush=True)
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "frames.ffconcat",
         "-vf", f"scale={W}:{H}:flags=lanczos,fps={FPS},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "14", "raw.mp4"], cwd=work)

    # 2) cards
    passed = sum(s["status"] == "PASS" for s in steps)
    failed = [s for s in steps if s["status"] != "PASS"]
    total = len(chapters)
    cards = {"title": (
        f"<div class='wrap'><div class='logo'>{LOGO}</div><div class='k'>M1 Local Lab · Demo</div>"
        "<h1>Agent Studio</h1><h2>本地优先的多智能体工作流平台：搭建 · 运行 · 观测 · 调试<br>并接入外部 LangGraph 智能体与 OTLP 轨迹</h2>"
        "<div class='chips'>" + "".join(f"<span class='chip'>{html.escape(c['title'])}</span>" for c in chapters) + "</div></div>"
        f"<div class='note'>真实运行录屏（Playwright + Chromium），全程未加速、无静态页面替代。演示离线进行：工作流与智能体均使用 "
        f"deterministic（脚本化）提供者，没有调用真实大模型，界面中有对应标注。· build {html.escape(str(m.get('git', '')))} · "
        f"{html.escape(m['generated_at'][:10])}</div>")}
    for i, c in enumerate(chapters, 1):
        cards[f"ch{i:02d}"] = (f"<div class='wrap'><div class='k'>Chapter {i} / {total}</div><h1>{html.escape(c['title'])}</h1>"
                               f"<h2>{html.escape(c['subtitle'])}</h2></div><div class='num'>{i:02d}</div>")
    rows = "".join(
        f"<div class='row'><b class='{'ok' if s['status'] == 'PASS' else 'bad'}'>{'✓' if s['status'] == 'PASS' else '✗'}</b>"
        f"<span>{html.escape(s['step'])}</span></div>" for s in steps)
    verdict = (f"{passed} / {len(steps)} 个步骤通过实时校验" if not failed else
               f"{passed} / {len(steps)} 个步骤通过，{len(failed)} 个未通过（见 COVERAGE.md）")
    cards["end"] = (f"<div class='wrap' style='justify-content:flex-start;padding-top:90px'><div class='k'>Verified live</div>"
                    f"<h1 style='font-size:64px'>{verdict}</h1>"
                    f"<div class='steps'>{rows}</div></div>"
                    "<div class='note'>重新生成：<code>python scripts/demo/build_demo.py</code> · 脚本位于 scripts/demo/ · "
                    "覆盖清单 docs/demo/COVERAGE.md</div>")
    print("2/5 cards", flush=True)
    render_cards(cards, work)

    # 3) clips: title, (chapter card, chapter segment)*, end
    clips: list[tuple[Path, float]] = []
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", "-r", str(FPS)]

    def still(name: str, seconds: float) -> None:
        run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-t", f"{seconds}", "-i", f"{name}.png",
             "-vf", f"scale={W}:{H},zoompan=z='min(1.0+0.0004*on,1.04)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                    f":d=1:s={W}x{H}:fps={FPS},format=yuv420p", *enc, f"{name}.mp4"], cwd=work)
        clips.append((work / f"{name}.mp4", probe_duration(work / f"{name}.mp4")))

    print("3/5 clips", flush=True)
    still("title", TITLE_S)
    raw_len = probe_duration(work / "raw.mp4")
    seg_info = []
    for i, c in enumerate(chapters, 1):
        still(f"ch{i:02d}", CARD_S)
        start = c["t"] - t0
        end = (chapters[i]["t"] - t0) if i < len(chapters) else raw_len
        length = end - start
        caps = [{**cap, "rel": cap["t"] - c["t"]} for cap in m["captions"] if cap["chapter"] == c["key"]]
        segment_ass(work / f"seg{i:02d}.ass", length, i, total, c, caps)
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", "raw.mp4",
             "-vf", f"subtitles=seg{i:02d}.ass,format=yuv420p", *enc, f"seg{i:02d}.mp4"], cwd=work)
        clips.append((work / f"seg{i:02d}.mp4", probe_duration(work / f"seg{i:02d}.mp4")))
        seg_info.append({"chapter": c, "start_raw": start, "length": length, "caps": caps})
    still("end", END_S)

    # 4) cross-fade everything into the final video
    print("4/5 final video", flush=True)
    inputs, filters, offset, prev = [], [], 0.0, "0:v"
    clip_starts = []
    for idx, (path, length) in enumerate(clips):
        inputs += ["-i", path.name]
        filters.append(f"[{idx}:v]settb=AVTB,fps={FPS},format=yuv420p[v{idx}]")
    acc = clips[0][1]
    clip_starts.append(0.0)
    prev = "v0"
    for idx in range(1, len(clips)):
        offset = acc - XFADE
        clip_starts.append(offset)
        transition = "fade" if idx % 2 else "smoothleft"
        filters.append(f"[{prev}][v{idx}]xfade=transition={transition}:duration={XFADE}:offset={offset:.3f}[x{idx}]")
        prev = f"x{idx}"
        acc = offset + clips[idx][1]
    final = out / "agent-studio-demo.mp4"
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filters), "-map", f"[{prev}]",
         "-c:v", "libx264", "-preset", "slow", "-tune", "stillimage", "-crf", "28", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(final)], cwd=work)   # UI footage: CRF 28 stays sharp, ~2.5 MB per minute
    final_len = probe_duration(final)
    # chapter markers (shown by VLC, IINA, mpv, QuickTime ...): each chapter starts at its card
    marks = [("片头 / Title", 0.0)] + [(f"{i}. {c['title']}", clip_starts[2 * i - 1]) for i, c in enumerate(chapters, 1)] \
        + [("结果 / Results", clip_starts[-1])]
    meta = [";FFMETADATA1", "title=Agent Studio walkthrough"]
    for j, (title, start) in enumerate(marks):
        end = marks[j + 1][1] if j + 1 < len(marks) else final_len
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(end * 1000)}", f"title={title}"]
    (work / "chapters.txt").write_text("\n".join(meta) + "\n", encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-i", str(final), "-i", "chapters.txt", "-map", "0", "-map_metadata", "1",
         "-map_chapters", "1", "-c", "copy", "-movflags", "+faststart", "final-chapters.mp4"], cwd=work)
    shutil.move(str(work / "final-chapters.mp4"), str(final))

    # 5) short preview from highlighted captions (cut from the final video, so captions are included)
    print("5/5 preview", flush=True)
    picks = []
    for k, info in enumerate(seg_info):
        seg_start = clip_starts[1 + 2 * k + 1]
        caps = info["caps"]
        for j, cap in enumerate(caps):
            if not cap.get("highlight") and cap["kind"] != "error":
                continue
            nxt = caps[j + 1]["rel"] if j + 1 < len(caps) else info["length"]
            dur = max(2.4, min(3.2, nxt - cap["rel"] + 0.4))
            picks.append((seg_start + cap["rel"] + 0.1, dur, cap["kind"] == "error"))
    if len(picks) > MAX_CUTS:  # keep every failure, spread the remaining highlights evenly over the video
        keep = {i for i, p in enumerate(picks) if p[2]}
        others = [i for i in range(len(picks)) if i not in keep]
        room = max(0, MAX_CUTS - len(keep))
        keep |= {others[round(j * (len(others) - 1) / max(room - 1, 1))] for j in range(room)} if room else set()
        picks = [p for i, p in enumerate(picks) if i in keep]
    cuts = [(clip_starts[0] + 0.6, 3.0)] + [(s0, dur) for s0, dur, _ in picks] + [(clip_starts[-1] + 0.8, 3.0)]
    parts = []
    for n, (s, dur) in enumerate(cuts):
        name = f"cut{n:02d}.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.3f}", "-t", f"{dur:.3f}", "-i", str(final), "-an",
             "-vf", "scale=1280:-2:flags=lanczos,fps=15,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "23", name], cwd=work)
        parts.append(name)
    (work / "cuts.txt").write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    preview_mp4 = out / "agent-studio-preview.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "cuts.txt", "-c:v", "libx264",
         "-preset", "medium", "-crf", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(preview_mp4)], cwd=work)
    gif = out / "agent-studio-preview.gif"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(preview_mp4), "-vf",
         "fps=7,scale=880:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];"
         "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", str(gif)], cwd=work)

    write_srt(out / "agent-studio-demo.srt", seg_info, clip_starts, [length for _, length in clips])
    # coverage report + manifest copy
    write_coverage(out / "COVERAGE.md", m, final_len, seg_info, clip_starts)
    slim = {k: v for k, v in m.items() if k != "recording"}
    slim["recording"] = {"frames": len(frames), "real_time_seconds": round(t_end - t0, 1)}
    slim["outputs"] = {"video": final.name, "video_seconds": round(final_len, 1), "preview_gif": gif.name,
                       "preview_mp4": preview_mp4.name, "preview_seconds": round(probe_duration(preview_mp4), 1)}
    for s in slim.get("steps", []):
        s.pop("traceback", None) if s["status"] == "PASS" else None
    (out / "demo-manifest.json").write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    mb = lambda p: f"{p.stat().st_size / 1e6:.1f} MB"  # noqa: E731
    print(f"\n{final} ({final_len:.1f}s, {mb(final)})\n{gif} ({mb(gif)})\n{preview_mp4} ({mb(preview_mp4)})\n"
          f"{out / 'COVERAGE.md'}", flush=True)
    return 0 if not failed else 2


def write_coverage(path: Path, m: dict, final_len: float, seg_info: list[dict], clip_starts: list[float]) -> None:
    steps = m["steps"]
    passed = sum(s["status"] == "PASS" for s in steps)
    mmss = lambda t: f"{int(t // 60)}:{int(t % 60):02d}"  # noqa: E731
    lines = [
        "# Demo video · feature coverage", "",
        "Generated by `scripts/demo/make_video.py` from the step results recorded by `scripts/demo/record_demo.py`. "
        "Do not edit by hand; regenerate with `python scripts/demo/build_demo.py`.", "",
        f"- Recorded: {m['generated_at']} · build `{m.get('git', '')}` · {m.get('platform', '')} · Chromium {m.get('chromium', '')}",
        f"- Providers: {m.get('providers', '')}",
        f"- Result: **{passed}/{len(steps)} steps passed live** · video {mmss(final_len)} "
        f"({final_len:.0f}s), recorded in real time without speed-up",
        f"- Browser errors during recording: console {len(m.get('browser_errors', {}).get('console', []))}, "
        f"page {len(m.get('browser_errors', {}).get('page', []))}, HTTP ≥400 {len(m.get('browser_errors', {}).get('http_errors', []))}",
        "", "| # | Chapter (video time) | Step verified in the running UI / API | Result |", "|---|---|---|---|",
    ]
    for k, info in enumerate(seg_info):
        c = info["chapter"]
        at = clip_starts[1 + 2 * k]
        chapter_steps = [s for s in steps if s["chapter"] == c["key"]] or [{"step": "(no steps recorded)", "status": "—"}]
        for j, s in enumerate(chapter_steps):
            label = f"{k + 1}. {c['title']} ({mmss(at)})" if j == 0 else ""
            res = "✅ PASS" if s["status"] == "PASS" else ("❌ FAIL — " + s.get("error", "")) if s["status"] == "FAIL" else s["status"]
            ids = ", ".join(f"{key}=`{s[key]}`" for key in ("run_id", "fork_id", "workflow_id") if key in s)
            step_text = s["step"].replace("|", "\\|")
            lines.append(f"| {k + 1 if j == 0 else ''} | {label} | {step_text}{' · ' + ids if ids else ''} | {res.replace('|', '/')} |")
    failed = [s for s in steps if s["status"] != "PASS"]
    if failed:
        lines += ["", "## Failures (shown in the video, not hidden)", ""]
        for s in failed:
            lines += [f"### {s['step']}", "", f"{s.get('error', '')}", "", "```", s.get("traceback", "").strip(), "```", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

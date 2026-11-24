#!/usr/bin/env python3
"""Build docs/gh-review-loop-demo.gif.

The demo is a staged Claude Code session, but the loop's own lines are not
hand-written: every script-owned block (thread header, Patterns, the sweep
report, the verification profile, receipt pointers, the PR receipt) is
produced here by calling the shipping formatters on the reviewer findings
captured from PR #67 (the same fixtures evals/replay/ replays). Change a
formatter and the demo changes with it on the next build.

    python3 -m venv /tmp/demo-venv && /tmp/demo-venv/bin/pip install pillow
    /tmp/demo-venv/bin/python docs/demo/build_demo.py

Needs Pillow and a local Chrome/Chromium (set CHROME to its binary if it is
not in the default macOS or PATH location). Each frame is an HTML page that
headless Chrome screenshots; Pillow assembles the GIF. Pass --keep to keep the
frame directory, --only N to render a single frame while editing.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SCRIPTS = REPO_ROOT / "plugins/gh-review-loop/skills/gh-review-loop/scripts"
FIXTURES = REPO_ROOT / "evals/replay/fixtures"
ICON = REPO_ROOT / "plugins/gh-review-loop/assets/icon.png"
OUT = REPO_ROOT / "docs/gh-review-loop-demo.gif"

sys.path.insert(0, str(SCRIPTS))

import cluster_findings
import fetch_gemini_threads
import metrics
import sweep_siblings

WIDTH, HEIGHT = 1200, 760
PALETTE_SIZE = 128

REPO = "OrenAshkenazy/gh-review-loop"
PR_NUMBER = 67
PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"
REVIEWER_LOGIN = "sourcery-ai"
REVIEWER_NAME = "Sourcery"
# Staged values: the fix commit and receipt comment of the run being depicted.
FIX_SHA = "9c41e07"
RECEIPT_URL = f"{PR_URL}#issuecomment-4391207715"
CAP = 3
SCRIPT_PATH = '"$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts'

STAGES = ["fetch", "cluster", "sweep", "fix", "verify", "push", "re-review", "clean"]


# ---------------------------------------------------------------------------
# Real loop output, generated from the captured review
# ---------------------------------------------------------------------------


def loop_output() -> dict[str, Any]:
    payload = json.loads((FIXTURES / "run1.json").read_text(encoding="utf-8"))
    threads = payload["threads"]
    clusters = cluster_findings.cluster(threads, root=FIXTURES / "src")
    multi = [c for c in clusters if c.count >= sweep_siblings.MIN_FLAGGED_SITES]
    sweep = sweep_siblings.sweep(
        signature=multi[0].signature,
        label=multi[0].label,
        sites=list(multi[0].sites),
        changed_files=payload["changed_files"],
        root=FIXTURES / "src",
    )
    siblings = sum(1 for line in sweep_siblings.render_report(sweep).splitlines()
                   if line.lstrip().startswith("+ "))

    profile = {
        "source": "confirmed",
        "detected_stack": "python",
        "working_directory": ".",
        "timeout_seconds": 300,
        "checks": [{"name": "tests", "command": "pytest", "required": True}],
    }
    details = {
        "verification": "passed",
        "failed_check": None,
        "failed_required": [],
        "checks": [{"name": "tests", "command": "pytest", "required": True,
                    "status": "passed", "returncode": 0, "duration_s": 13.4}],
    }
    pr = {"number": PR_NUMBER, "url": PR_URL, "reviews": {"nodes": []},
          "comments": {"nodes": []}, "reviewThreads": {"nodes": []}}
    header = fetch_gemini_threads.render_markdown(
        pr, [], REVIEWER_LOGIN, reviewer_name=REVIEWER_NAME,
        review_trigger_mention=f"@{REVIEWER_LOGIN} review",
    ).splitlines()
    # The header is rendered without threads so the fixture's flattened thread
    # shape never reaches the raw-PR helpers; restore the real count.
    header = [f"Threads: {len(threads)}" if line.startswith("Threads:") else line
              for line in header if line.strip()]

    base = {"findings_fetched": len(threads), "fixed_count": len(threads),
            "cycle_cap": CAP, "verification": "passed", "session_cycle": 1}
    cycle = {**base, "remaining_actionable": len(threads), "cycles_used": 0}
    terminal = {**base, "remaining_actionable": 0, "cycles_used": 1,
                "outcome": "clean", "duration_seconds": 6 * 60 + 12,
                "terminal_breakdown": {"confirmed_fixed_outdated": len(threads)}}
    return {
        "threads": len(threads),
        "siblings": siblings,
        "header": header,
        "patterns": metrics.format_patterns_block(clusters).splitlines(),
        "sweep": sweep_siblings.render_report(sweep).splitlines(),
        "profile_intro": metrics.format_profile_intro_block(profile, REPO).splitlines(),
        "details": json.dumps(details, indent=2).splitlines(),
        "cycle_pointer": metrics.format_compact_receipt_line(
            cycle, terminal=False, receipt_url=RECEIPT_URL,
            findings_new=len(threads), findings_carried=0),
        "summary_pointer": metrics.format_compact_receipt_line(
            terminal, terminal=True, receipt_url=RECEIPT_URL),
        "receipt": (metrics.format_run_summary(terminal, terminal=True) + "\n\n"
                    + metrics.format_suite_block(details)).splitlines(),
    }


# ---------------------------------------------------------------------------
# Transcript items (Claude Code look)
# ---------------------------------------------------------------------------


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def loop_line(text: str) -> str:
    """Escape one line, tinting a leading [loop]/[sweep] tag like the scripts do."""
    for tag in ("[loop]", "[sweep]"):
        if text.startswith(tag):
            return f'<span class="tag">{tag}</span>{esc(text[len(tag):])}'
    return esc(text)


def user(text: str) -> str:
    return f'<div class="item user"><span class="chev">&gt;</span> {esc(text)}</div>'


def say(text: str) -> str:
    return (f'<div class="item say"><span class="dot"></span>'
            f'<div class="body">{loop_line(text)}</div></div>')


def tool(name: str, arg: str, out: list[str], *, more: str = "") -> str:
    rows = "".join(f'<div class="row">{line or "&nbsp;"}</div>' for line in out)
    if more:
        rows += f'<div class="row dim">{esc(more)}</div>'
    return (f'<div class="item tool"><span class="dot ok"></span><div class="body">'
            f'<div class="call"><b>{esc(name)}</b>({esc(arg)})</div>'
            f'<div class="out"><span class="elbow">⎿</span><div class="rows">{rows}</div></div>'
            f'</div></div>')


def diff(path: str, summary: str, rows: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<div class="row d{ {"+": "add", "-": "del"}.get(sign, "ctx") }">'
        f'<span class="ln">{num}</span><span class="sg">{sign or " "}</span>{esc(text)}</div>'
        for num, sign, text in rows
    )
    return tool("Update", path, [esc(summary)]).replace(
        "</div></div></div></div>", f'{body}</div></div></div></div>', 1)


def hl(line: str, cls: str) -> str:
    return f'<span class="{cls}">{line}</span>'


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


def timeline(o: dict[str, Any]) -> list[dict[str, Any]]:
    """One dict per GIF frame: the full visible state plus its hold time."""
    frames: list[dict[str, Any]] = []
    state: dict[str, Any] = {
        "items": [], "input": "", "spinner": "", "stage": -1, "cap": 0,
        "caption": "One prompt, from a PR your AI reviewer already commented on",
        "overlay": "",
    }

    def frame(ms: int, **changes: Any) -> None:
        add = changes.pop("add", None)
        state.update(changes)
        if add:
            state["items"] = state["items"] + [add]
        frames.append({**state, "ms": ms})

    prompt = "Run the AI reviewer loop"
    frame(700)
    for i in range(2, len(prompt) + 1, 2):
        frame(60, input=prompt[:i])
    frame(350, input=prompt)

    frame(400, input="", add=user(prompt), spinner="Loading skill… (2s · esc to interrupt)")
    frame(500, add=tool("Skill", "gh-review-loop:gh-review-loop", ["Successfully loaded skill"]))

    frame(600, stage=0, add=say(f"[loop] fetching threads from PR #{PR_NUMBER}..."),
          spinner="Fetching review threads… (9s · esc to interrupt)",
          caption="Reads real review-thread state, not flat PR comments")
    fetch_out = [loop_line("[loop] session cycle 1")]
    fetch_out += [esc(o["header"][0]), esc(o["header"][-1])]
    fetch_out += [hl(esc(line), "focus") if "sites" in line else esc(line) for line in o["patterns"]]
    fetch_out += [loop_line(line) for line in o["profile_intro"]]
    frame(2600, stage=1, add=tool(
        "Bash", f'python3 {SCRIPT_PATH}/fetch_gemini_threads.py"', fetch_out),
        spinner="Clustering findings… (14s · esc to interrupt)",
        caption="Clusters findings by pattern: two comments, one defect")
    frame(1400, add=say(
        f"[loop] session cycle 1 — re-review cap: 0/{CAP} consumed. "
        f"{o['threads']} actionable thread(s) (severity: {o['threads']} unknown). Fixing."))

    sweep_out = []
    for line in o["sweep"]:
        if line.lstrip().startswith("+ "):
            sweep_out.append(hl(esc(line), "sib"))
        elif line.lstrip().startswith("siblings:"):
            sweep_out.append(hl(esc(line), "focus"))
        else:
            sweep_out.append(loop_line(line))
    frame(3800, stage=2, add=tool(
        "Bash", f'python3 {SCRIPT_PATH}/sweep_siblings.py" --signature shape:802659ea …', sweep_out),
        spinner="Sweeping siblings… (31s · esc to interrupt)",
        caption=f"The reviewer flagged {o['threads']}. The sweep finds the other {o['siblings']}.")
    total = o["threads"] + o["siblings"]
    frame(1500, stage=3, add=say(
        f"The sweep reported {o['siblings']} unflagged siblings of the flagged pattern. "
        f"Fixing all {total} sites this cycle."),
        spinner="Fixing… (48s · esc to interrupt)",
        caption="Fixes the whole class in one cycle, not one instance per round")
    frame(1500, add=diff(
        "loaders/profiles.py", "Updated loaders/profiles.py with 13 additions and 3 removals",
        [("15", "", '    """Read one exported verification profile."""'),
         ("16", "-", '    data = json.loads(path.read_text(encoding="utf-8"))'),
         ("16", "+", "    data = _read_json_object(path)"),
         ("17", "", '    return data.get("profile", {})')]),
        spinner="Fixing… (1m 12s · esc to interrupt)")
    frame(700, add=tool("Update", "loaders/bundle.py",
                         ["Updated loaders/bundle.py with 4 additions and 3 removals"]))

    frame(700, stage=4, add=say("[loop] session cycle 1 — fixes applied. Verifying via profile runner."),
          spinner="Running pytest… (1m 20s · esc to interrupt)",
          caption="Won't push red: your repo's own tests gate every push")
    verify_out = [esc(line) for line in o["details"][:2]]
    frame(1100, add=tool(
        "Bash", f'python3 {SCRIPT_PATH}/run_profile.py" {REPO} .', verify_out,
        more=f"… +{len(o['details']) - 2} lines (ctrl+o to expand)"),
        spinner="Running pytest… (1m 41s · esc to interrupt)")
    frame(1000, add=say("[loop] session cycle 1 — verified (tests: pytest passed)."))

    frame(1700, stage=5, add=say(o["cycle_pointer"]),
          spinner="Pushing… (1m 49s · esc to interrupt)",
          caption="One receipt on the PR, one line in your terminal")
    frame(700, add=say(f"[loop] session cycle 1 — committing and pushing {FIX_SHA}..."))
    frame(1700, stage=6, cap=1, add=say(
        f"[loop] session cycle 1 — pushed. Requesting reviewer re-review. Cap now 1/{CAP}."),
        spinner="Waiting for the reviewer… (1m 58s · esc to interrupt)",
        caption=f"Re-review requested: 1 of {CAP}. A hard cap, not a timeout.")

    wait_arg = f'python3 {SCRIPT_PATH}/fetch_gemini_threads.py" --wait --after "$REREVIEW_AT" --timeout 1800'
    waiting = f"Waiting for {REVIEWER_LOGIN} review of {FIX_SHA}..."
    wait_lines = [
        f"{waiting} (0s elapsed, timeout 1800s)",
        f"{waiting} (63s elapsed, timeout 1800s)",
        f"{waiting} (127s elapsed, timeout 1800s)",
        f"Detected {REVIEWER_LOGIN} review activity; waiting for it to settle...",
        f"{REVIEWER_LOGIN} review activity is stable.",
    ]
    base_items = state["items"]
    for shown, clock in [(1, "2m 01s"), (3, "4m 08s"), (4, "5m 52s")]:
        state["items"] = base_items
        frame(550, add=tool("Bash", wait_arg, [esc(line) for line in wait_lines[:shown]]),
              spinner=f"Waiting for the reviewer… ({clock} · esc to interrupt)")
    state["items"] = base_items
    done_out = [esc(wait_lines[-1]), loop_line("[loop] session cycle 1"),
                esc(o["header"][0]), hl("Threads: 0", "ok")]
    frame(1300, stage=7, add=tool("Bash", wait_arg, done_out),
          spinner="Recording the run… (6m 08s · esc to interrupt)",
          caption="The reviewer re-reviews the new head and comes back clean")
    frame(1100, add=say(
        f"[loop] DONE — 0 actionable threads remaining. Re-review requests used: 1/{CAP}."))
    frame(1900, add=say(o["summary_pointer"]), spinner="", stage=8)

    frame(3400, overlay=receipt_card(o),
          caption="The full receipt lives on the PR, edited in place as the loop runs")
    frame(4200, overlay=end_card(o), caption="")
    return frames


def receipt_card(o: dict[str, Any]) -> str:
    code = "\n".join(loop_line(line) for line in o["receipt"])
    return f"""
<div class="scrim"></div>
<div class="ghcard">
  <div class="ghhead"><span class="avatar"></span><b>OrenAshkenazy</b> commented
    <span class="ghdim">· edited</span>
    <span class="ghright">{esc(REPO)} <b>#{PR_NUMBER}</b></span></div>
  <div class="ghbody">
    <h3>gh-review-loop receipt — <span class="done">DONE</span></h3>
    <pre>{code}</pre>
    <p class="ghdim"><i>This comment is edited in place as the loop progresses.</i></p>
  </div>
</div>"""


def end_card(o: dict[str, Any]) -> str:
    total = o["threads"] + o["siblings"]
    return f"""
<div class="scrim solid"></div>
<div class="end">
  <img src="{icon_uri()}" alt="">
  <div class="wordmark">gh-review-loop</div>
  <div class="headline">Your AI reviewer flagged {o['threads']}. <span class="blue">There were {total}.</span></div>
  <div class="facts">
    <span><i class="pip g"></i>all {total} sites fixed in one cycle</span>
    <span><i class="pip a"></i>pushed only on green tests</span>
    <span><i class="pip b"></i>1 of {CAP} re-reviews used</span>
  </div>
  <div class="install">/plugin marketplace add {REPO}<br>/plugin install gh-review-loop@gh-review-loop</div>
  <div class="foot">Claude Code · Codex · MIT · no server, no account</div>
</div>"""


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: __W__px; height: __H__px; overflow: hidden; background: #080b10; }
body { font-family: ui-monospace, "SF Mono", Menlo, monospace; color: #e6edf3;
       font-size: 16.5px; line-height: 24px; -webkit-font-smoothing: antialiased; }
.sans { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; }

.brand { position: absolute; left: 28px; right: 28px; top: 0; height: 64px;
         display: flex; align-items: center; gap: 12px; }
.brand img { width: 34px; height: 34px; border-radius: 8px; }
.brand .name { font-weight: 700; font-size: 21px; letter-spacing: -0.2px; }
.rail { margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 13px; }
.chip { padding: 1px 9px; border-radius: 999px; border: 1px solid #262c36; color: #5d6673; }
.chip.done { color: #3fb950; border-color: #1f4a2a; background: #0f2416; }
.chip.on { color: #0d1117; background: #58a6ff; border-color: #58a6ff; font-weight: 700; }
.cap { margin-left: 12px; display: flex; align-items: center; gap: 5px; color: #8b949e; }
.cap i { width: 11px; height: 11px; border-radius: 50%; border: 2px solid #3d4551; display: block; }
.cap i.used { background: #58a6ff; border-color: #58a6ff; }

.win { position: absolute; left: 28px; right: 28px; top: 64px; bottom: 62px;
       background: #0d1117; border: 1px solid #30363d; border-radius: 12px;
       display: flex; flex-direction: column; overflow: hidden; }
/* The session flows from the top like a fresh terminal; once it outgrows the
   window the auto margin collapses and flex-end keeps the newest lines in view. */
.view { flex: 1; min-height: 0; overflow: hidden; display: flex; flex-direction: column;
        justify-content: flex-end; }
.flow { flex: none; margin-bottom: auto; }
.bar { height: 36px; flex: none; background: #161b22; border-bottom: 1px solid #30363d;
       display: flex; align-items: center; padding: 0 14px; gap: 8px;
       color: #8b949e; font-size: 13px; }
.bar i { width: 12px; height: 12px; border-radius: 50%; display: block; }
.bar .t { margin-left: 10px; }
.log { padding: 0 18px; }
.item { margin-top: 12px; display: flex; gap: 10px; }
.item .body { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.user { color: #9aa4af; background: #151b23; margin-left: -18px; margin-right: -18px;
        padding: 3px 18px; display: block; }
.user .chev { color: #6e7681; }
.dot { flex: none; width: 10px; height: 10px; border-radius: 50%; background: #e6edf3;
       margin-top: 7px; }
.dot.ok { background: #3fb950; }
.tag { color: #d2a8ff; font-weight: 700; }
.call { color: #e6edf3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.out { display: flex; gap: 10px; color: #9aa4af; }
.elbow { flex: none; color: #6e7681; }
.rows { flex: 1; min-width: 0; }
.row { white-space: pre; overflow: hidden; text-overflow: ellipsis; }
.dim { color: #6e7681; }
.focus { color: #ffffff; font-weight: 700; background: #1c2a3f; }
.sib { color: #3fb950; font-weight: 700; }
.ok { color: #3fb950; font-weight: 700; }
.row.dadd { background: #12301c; color: #aff5b4; }
.row.ddel { background: #3c1618; color: #ffc1ba; }
.row.dctx { color: #9aa4af; }
.ln { display: inline-block; width: 34px; text-align: right; color: #6e7681; }
.sg { display: inline-block; width: 22px; text-align: center; }

.spin { height: 30px; margin-top: 10px; padding: 0 18px; color: #d97757; }
.spin b { font-weight: 400; color: #8b949e; }
.inp { margin: 4px 14px 6px; border: 1px solid #3d4551; border-radius: 8px;
       padding: 6px 12px; height: 38px; color: #e6edf3; white-space: pre; }
.inp .chev { color: #8b949e; }
.cursor { display: inline-block; width: 9px; height: 19px; background: #e6edf3;
          vertical-align: -4px; }
.hint { height: 26px; padding: 0 26px; color: #6e7681; font-size: 13px; }

.caption { position: absolute; left: 0; right: 0; bottom: 0; height: 62px;
           display: flex; align-items: center; justify-content: center;
           font-size: 21px; font-weight: 600; color: #e6edf3; letter-spacing: -0.1px; }

.scrim { position: absolute; left: 0; right: 0; top: 64px; bottom: 62px; background: #080b10;
         opacity: 0.86; }
.scrim.solid { top: 0; bottom: 0; opacity: 1; }
.ghcard { position: absolute; left: 190px; right: 190px; top: 126px;
          border: 1px solid #3d444d; border-radius: 10px; background: #0d1117; overflow: hidden; }
.ghhead { background: #151b23; border-bottom: 1px solid #3d444d; padding: 10px 16px;
          display: flex; align-items: center; gap: 8px; color: #9198a1; font-size: 15px;
          font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; }
.ghhead b { color: #f0f6fc; }
.ghright { margin-left: auto; }
.avatar { width: 24px; height: 24px; border-radius: 50%; background: #58a6ff; display: block; }
.ghbody { padding: 16px 20px 18px; font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; }
.ghbody h3 { font-size: 21px; margin-bottom: 12px; color: #f0f6fc; }
.ghbody .done { color: #3fb950; }
.ghbody pre { background: #151b23; border-radius: 8px; padding: 14px 16px; font-size: 15px;
              line-height: 22px; font-family: ui-monospace, "SF Mono", Menlo, monospace;
              color: #e6edf3; margin-bottom: 12px; }
.ghdim { color: #9198a1; font-size: 14px; }

.end { position: absolute; inset: 0; display: flex; flex-direction: column;
       align-items: center; justify-content: center; text-align: center; }
.end img { width: 104px; height: 104px; border-radius: 24px; }
.end .wordmark { font-size: 34px; font-weight: 700; margin-top: 18px; letter-spacing: -0.5px; }
.end .headline { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
                 font-size: 40px; line-height: 50px; font-weight: 800; margin-top: 26px;
                 letter-spacing: -0.6px; }
.blue { color: #58a6ff; }
.facts { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; margin-top: 22px;
         display: flex; gap: 30px; font-size: 19px; color: #c9d1d9; }
.facts span { display: flex; align-items: center; gap: 9px; }
.pip { width: 11px; height: 11px; border-radius: 50%; display: block; }
.pip.g { background: #3fb950; } .pip.a { background: #d29922; } .pip.b { background: #58a6ff; }
.install { margin-top: 34px; text-align: left; background: #0d1117; border: 1px solid #30363d;
           border-radius: 10px; padding: 14px 22px; color: #58a6ff; font-size: 18px;
           line-height: 29px; }
.foot { margin-top: 20px; color: #8b949e; font-size: 15px; }
""".replace("__W__", str(WIDTH)).replace("__H__", str(HEIGHT))


def icon_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(ICON.read_bytes()).decode()


def page(state: dict[str, Any]) -> str:
    chips = []
    for index, name in enumerate(STAGES):
        cls = "done" if index < state["stage"] else "on" if index == state["stage"] else ""
        chips.append(f'<span class="chip {cls}">{name}</span>')
    pips = "".join(f'<i class="{"used" if n < state["cap"] else ""}"></i>' for n in range(CAP))
    spinner = state["spinner"]
    if spinner:
        verb, _, rest = spinner.partition(" (")
        spinner = f'✻ {esc(verb)} <b>({esc(rest)}</b>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<div class="brand"><img src="{icon_uri()}" alt=""><span class="name">gh-review-loop</span>
  <div class="rail">{''.join(chips)}<span class="cap">cap {pips}</span></div></div>
<div class="win">
  <div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i>
    <span class="t">claude · gh-review-loop · PR #{PR_NUMBER} demo/sweep-proof</span></div>
  <div class="view"><div class="flow">
    <div class="log">{''.join(state['items'])}</div>
    <div class="spin">{spinner}</div>
    <div class="inp"><span class="chev">&gt;</span> {esc(state['input'])}<span class="cursor"></span></div>
    <div class="hint">? for shortcuts</div>
  </div></div>
</div>
<div class="caption sans">{esc(state['caption'])}</div>
{state['overlay']}
</body></html>"""


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def find_chrome() -> str:
    candidates = [
        os.environ.get("CHROME"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("error: no Chrome/Chromium found; set CHROME to the binary path")


def screenshot(chrome: str, html_path: Path, png_path: Path, profile_dir: Path) -> None:
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
         "--force-device-scale-factor=1", f"--window-size={WIDTH},{HEIGHT}",
         f"--user-data-dir={profile_dir}", f"--screenshot={png_path}", html_path.as_uri()],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the frame directory")
    parser.add_argument("--only", type=int, help="render one frame index to a PNG and stop")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("error: Pillow is required (pip install pillow)") from None

    frames = timeline(loop_output())
    chrome = find_chrome()
    work = Path(tempfile.mkdtemp(prefix="grl-demo-"))
    indexes = [args.only] if args.only is not None else list(range(len(frames)))

    def render(index: int) -> Path:
        html_path = work / f"f{index:03d}.html"
        png_path = work / f"f{index:03d}.png"
        html_path.write_text(page(frames[index]), encoding="utf-8")
        screenshot(chrome, html_path, png_path, work / f"profile{index:03d}")
        shutil.rmtree(work / f"profile{index:03d}", ignore_errors=True)
        return png_path

    with ThreadPoolExecutor(max_workers=6) as pool:
        pngs = list(pool.map(render, indexes))

    if args.only is not None:
        print(pngs[0])
        return 0

    images = [Image.open(p).convert("RGB") for p in pngs]
    sizes = {im.size for im in images}
    if sizes != {(WIDTH, HEIGHT)}:
        raise SystemExit(f"error: unexpected frame sizes {sizes}")
    # One palette for every frame, so unchanged pixels stay byte-identical and
    # Pillow's frame differencing keeps each frame down to what actually moved.
    # It is the most frequent exact colours across the whole run rather than a
    # median cut: this is flat UI, and a median cut spends its slots on text
    # anti-aliasing and drops the small accents (traffic lights, the [loop] tag).
    counts: Counter[tuple[int, int, int]] = Counter()
    for im in images:
        for count, colour in im.getcolors(maxcolors=WIDTH * HEIGHT):
            counts[colour] += count
    palette = Image.new("P", (1, 1))
    palette.putpalette([channel for colour, _ in counts.most_common(PALETTE_SIZE)
                        for channel in colour])
    quantized = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    quantized[0].save(
        args.out, save_all=True, append_images=quantized[1:], loop=0, optimize=False,
        duration=[frame["ms"] for frame in frames], disposal=1,
    )
    total = sum(frame["ms"] for frame in frames) / 1000
    print(f"{args.out}: {len(frames)} frames, {total:.1f}s, {args.out.stat().st_size / 1024:.0f} KB")
    if args.keep:
        print(f"frames kept in {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

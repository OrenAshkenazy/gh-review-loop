# Optional Judge Eval (`--judge-mode`)

An optional OpenAI-based judge classifies each reviewer finding as `valid_actionable / false_positive / duplicate / already_addressed / explanation_only / needs_human`, plus a `severity_override` and `recommended_action`. The judge is **read-only** — it never resolves threads, posts comments, or pushes.

**Off by default.** Nothing is sent to OpenAI unless the user explicitly opts in.

## API key

Resolved by `key_resolver.py` (env var → dotfile → macOS Keychain → Linux Secret Service). No SDK needed — stdlib `urllib`. Missing key → the judge skips with a structured `skipped` result + one stderr hint; the loop continues unchanged.

To set the key permanently, recommend the OS-keystore path:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/key_resolver.py" --set
```

Stores in macOS Keychain, Linux Secret Service, or a dotfile fallback (chmod 600 on POSIX; Windows access depends on the user-directory ACL). No rc-file edits or `ps` leakage. Diagnostics: `key_resolver.py --print-source`, `judge_doctor.py`.

## Modes and phase

`fetch_gemini_threads.py` is the single source of truth: it reads the prefs file on every invocation and combines the saved mode with the phase.

**Phase is auto-inferred** — no `--judge-phase` needed: a terminal `--record-run` is phase `complete`; every other fetch is phase `cycle`. So `on_cycle` runs on every cycle fetch and `on_complete` runs only at the terminal record-run. An explicit `--judge-phase` overrides.

When `judge_mode=on_cycle`, show the deterministic judge block every cycle. If the judge was requested but skipped, relay the script-owned line exactly (`[loop] judge eval skipped: <reason>`). Never hand-write a replacement judge table.

## Discoverability

Do **not** prompt for judge eval during a normal loop run or at session start.

**One-time tip — after fetch, before fixes.** On the first cycle with actionable findings, if `judge_tip_shown` is not `true` in prefs, emit immediately after the findings narration line, then call `mark_tip_shown()`:

```
[loop] cycle 1/<cap> — 4 actionable thread(s) (high: 1, medium: 3). Fixing.
[loop] Tip: judge eval can give a second opinion on these findings.
         Try: "run the review loop with judge eval at completion"
```

The tip appears exactly once across all future sessions.

## When to prompt

Only when the user explicitly requests judge eval **without specifying a mode** ("enable judge eval" / "use judge eval" / "turn on eval"). Use the runtime's choice-prompt mechanism:

> Judge eval sends reviewer findings and related PR context to OpenAI.
>
> Choose eval mode:
> 1. Every cycle
> 2. At completion only
> 3. Just this once
> 4. Off

Mapping: 1 → `save_preferences("on_cycle")`; 2 → `save_preferences("on_complete")`; 3 → do NOT save, pass `--judge-mode once --judge-phase complete` for this run only; 4 → `save_preferences("off")`.

Mode-specific phrasings need no prompt — see `references/variations.md`.

## Preference file

`~/.config/gh-review-loop/preferences.json` — created automatically on first script invocation with safe defaults.

```json
{
  "schema_version": 2,
  "judge_mode": "off",
  "judge_tip_shown": true,
  "max_rereview_requests": 3
}
```

- `judge_mode` — `off` / `on_complete` / `on_cycle`. Default `off`.
- `max_rereview_requests` — persistent loop cap (default 3). Per-run override: `--max-rereview-requests N`.
- `judge_tip_shown` — internal; set automatically.
- `judge_model` — OpenAI model for eval. Default `gpt-4o-mini`.

To change the persistent cap, edit the JSON directly or:

```bash
python3 - <<'PY'
import json
from pathlib import Path

# Resolve like the scripts do: new dir, else unmigrated legacy dir.
base = Path.home() / ".config" / "gh-review-loop"
legacy = Path.home() / ".config" / "gh-gemini-review-loop"
if not base.exists() and legacy.exists():
    base = legacy
path = base / "preferences.json"
path.parent.mkdir(parents=True, exist_ok=True)
prefs = json.loads(path.read_text()) if path.exists() else {}
prefs["schema_version"] = 2
prefs["max_rereview_requests"] = 4
path.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n")
PY
```

## Cost framing

`gpt-4o-mini` ≈ $0.001 per finding. `on_complete` ≈ $0.005 max per PR. `on_cycle` worst case scales with the cap (default ≈ $0.015 for 3 cycles × 5 findings).

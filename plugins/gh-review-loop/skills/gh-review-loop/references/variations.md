# Variations (user-prompt → flag mapping)

This table is authoritative; if a phrasing isn't here, fall back to defaults.

| User intent | Phrasing examples | Pass to script |
|---|---|---|
| **Default loop** | "run the AI reviewer loop" / "handle reviewer feedback" / "run the gemini loop" / "yeet this PR" | (no extra flags) |
| **High-severity only** | "only fix high severity" / "skip the nits" / "just the important stuff" | `--min-severity high` |
| **Medium and above** | "skip low-priority comments" | `--min-severity medium` |
| **Critical only** | "just the critical findings" | `--min-severity critical` |
| **Strict severity filter** | "only what the reviewer flagged as high — ignore unmarked" | `--min-severity high --drop-unknown-severity` |
| **Audit-only** | "summarize reviewer comments" / "read-only review" / "show me what's pending" | `--dry-run --post-receipt --no-resolve-outdated --no-resolve-addressed-by-reply` |
| **More cycles once** | "be persistent" / "do 4 cycles" | `--max-rereview-requests 4` |
| **Fewer cycles once** | "one cycle only" / "don't loop, just fix once" | `--max-rereview-requests 1` |
| **Persistent cap** | "always use 4 cycles" / "configure the cap max to 4" | Set `max_rereview_requests` in `~/.config/gh-review-loop/preferences.json` |
| **Specific PR** | "handle PR https://github.com/..." | `--pr <URL>` |
| **Select Codex** | "run the Codex loop" / "use Codex for this PR" | `--reviewer chatgpt-codex-connector --reviewer-source confirmed` (name and trigger are known) |
| **Different reviewer bot** | "handle review comments from coderabbitai" | `--reviewer coderabbitai --review-trigger-mention @coderabbitai --reviewer-name CodeRabbit` |
| **Post status without acting** | "leave a status comment without touching anything" | `--post-receipt --no-resolve-outdated --no-resolve-addressed-by-reply` |
| **Live status comment** | "show me a live status comment on the PR" / "I want background visibility" | `--sticky-receipt --receipt-status running` per cycle; `--sticky-receipt --receipt-status done` at the final invocation |
| **Loop + judge at completion** | "run the AI reviewer loop with judge eval at completion" | `save_preferences("on_complete")`. Phase auto-inferred. No prompt. |
| **Loop + judge every cycle** | "with judge eval on every cycle" | `save_preferences("on_cycle")`. Phase auto-inferred. No prompt. |
| **Judge just this once** | "run judge eval just this once" | `--judge-mode once --judge-phase complete`. No save. No prompt. |
| **Enable judge eval (no mode)** | "enable judge eval" / "use judge eval" / "turn on eval" | Show the runtime choice prompt; act on answer. |
| **Explain judge eval** | "what is judge eval?" / "how does judge eval work?" | Explain it. Do not enable it. |
| **Disable judge for this run** | "skip the judge this time" | `--judge-mode off` |
| **Change saved preference** | "change my eval preference" / "reset judge mode" | Show the runtime choice prompt; overwrite prefs file. |
| **Default loop with saved judge mode** | (no special phrasing) | No `--judge-phase` needed — phase auto-inferred. Script obeys saved mode. |
| **History investigation** | "show me all reviewer threads ever, including resolved" | `--include-resolved --include-outdated --include-addressed-by-reply --no-resolve-outdated --no-resolve-addressed-by-reply` |
| **Local stats** | "show reviewer loop stats" / "loop stats for this repo" | `--stats` |
| **Set up verification profile** | "set up a verification profile for this repo" / "configure checks for this repo" | Run `detect_profile.py`, show preset menu, persist the chosen preset |
| **Customize profile** | "add mypy to this repo's checks" / "change the verification checks to X" | Edit checks, `save_profile(..., source="customized")` |
| **Skip profile** | "skip verification profile" / "use ad-hoc checks for this repo" | `save_profile(repo, source="skipped")` — suppress automatic re-prompt |

Resolution is off by default. Explicit user opt-in enables `--resolve-outdated` / `--resolve-addressed-by-reply`; respect later opt-out for the rest of the session.

One configured reviewer bot per run — the loop does not aggregate several reviewers. Severity parsing reads markdown image alt text (shared scale and Codex `P0`–`P3`); reviewers without either marker fall back to `unknown`.

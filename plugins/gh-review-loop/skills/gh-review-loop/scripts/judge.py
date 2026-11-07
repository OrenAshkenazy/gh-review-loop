"""End-user-side LLM-as-judge for AI reviewer findings.

This module ships INSIDE the installed plugin (it is what the user invokes
via ``--judge-mode``). It is intentionally separate from ``evals/judge.py``
at the repo root — that one is the maintainer's calibration judge and uses
a simpler 4-label schema. This one uses the richer end-user schema and runs
on the user's own PRs.

Verdict schema (richer than the maintainer one because end-users need
actionable advice, not just calibration labels):

- ``valid_actionable``   — Real finding the user should act on.
- ``false_positive``     — Incorrect; misread the diff or wrong context.
- ``needs_human``        — Requires a product/design/security judgment.
- ``explanation_only``   — Asks for an explanation, not a code change.
- ``duplicate``          — Substantively duplicates another finding on this PR.
- ``already_addressed``  — Resolved elsewhere; the line just hasn't been auto-resolved.

Plus:
- ``severity_override``  — One of critical/high/medium/low/none. Judge's
  opinion on the right severity, which can differ from the reviewer's label.
- ``recommended_action`` — fix / reply / ignore / escalate.

Privacy / cost / safety invariants
- This module makes ZERO calls to OpenAI on its own. It only does work
  when the caller passes a judge mode and a phase that match. The caller
  (fetch_gemini_threads.py) reads the user's saved preference file and
  decides; this module obeys.
- The judge NEVER mutates the GitHub state. It cannot resolve, comment,
  push, or call any GraphQL/REST write. It only reads finding text and
  emits structured verdicts.
- When the OpenAI key is missing, this module returns a structured
  ``skipped`` result with a reason — not a raw exception. The caller
  surfaces the reason and continues the loop unchanged.
- The HTTP call uses stdlib ``urllib`` only (no ``openai`` SDK). Avoids
  the install-fragility class of failures (broken interpreters, pipx
  breakage, externally-managed envs). Trade-off: we re-implement one
  endpoint's request shape; we don't get streaming, retries with
  Retry-After, or vision. Acceptable for a single-shot JSON judge call.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import typing as t
from pathlib import Path
from urllib import error as _urlerror
from urllib import request as _urlrequest

import loop_state  # noqa: E402 — sibling module, stdlib-only

# Imported as a sibling module so the test invariant (no shell-out from
# judge.py) still holds — keychain / secret-tool reads live in
# key_resolver.py, which judge.py only consults via a pure function call.
try:
    from key_resolver import resolve_api_key as _resolve_api_key  # noqa: PLC0415
except ImportError:  # pragma: no cover — fallback when run outside the plugin tree
    def _resolve_api_key() -> tuple[str | None, str]:
        return os.environ.get("OPENAI_API_KEY"), "env" if os.environ.get("OPENAI_API_KEY") else "missing"


# Detect API keys that look like placeholders rather than real OpenAI keys.
# Real keys begin with `sk-` (or `sk-svcacct-`, `sk-proj-`, `sk-admin-`) and
# are at least ~40 chars. Anything shorter or matching these obvious
# placeholder phrases is rejected before the SDK ever sees it, so users get
# a clear error instead of a confusing 401 with the placeholder echoed back.
_PLACEHOLDER_MARKERS = (
    "REPLACE_",
    "YOUR_KEY",
    "YOUR-KEY",
    "PASTE_",
    "INSERT_",
    "TODO",
    "XXX",
    "CHANGEME",
    "<your",
)
_MIN_REAL_KEY_LEN = 40


VALID_VERDICTS = (
    "valid_actionable",
    "false_positive",
    "needs_human",
    "explanation_only",
    "duplicate",
    "already_addressed",
)

VALID_SEVERITY_OVERRIDES = ("critical", "high", "medium", "low", "none")

VALID_RECOMMENDED_ACTIONS = ("fix", "reply", "ignore", "escalate")

VALID_JUDGE_MODES = ("off", "on_cycle", "on_complete", "once")

VALID_JUDGE_PHASES = ("cycle", "complete")

DEFAULT_MODEL = os.environ.get("OPENAI_JUDGE_MODEL", "gpt-4o-mini")
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_MAX_REREVIEW_REQUESTS = 3

# Endpoint can be overridden via OPENAI_BASE_URL for self-hosted gateways
# (Ollama, LiteLLM, LM Studio, enterprise proxies). Mirrors the SDK's
# OPENAI_BASE_URL contract so users who set it for the SDK don't have to
# learn a new var when we drop the SDK dep.
DEFAULT_BASE_URL = "https://api.openai.com/v1"

PREFS_SCHEMA_VERSION = 2

SYSTEM_PROMPT = """\
You are an expert code-review judge. The user is deciding whether to act on \
a finding posted by an AI code reviewer on their pull request. Be strict: do \
not assume the reviewer is correct just because it sounds confident, and do \
not assume it is wrong just because the finding is minor.

Classify the finding as exactly ONE verdict:
- "valid_actionable": Real issue the user should act on (bug, security, \
correctness, meaningful style/clarity improvement, missing test, doc/code \
mismatch, defensive null-safety on a plausible path).
- "false_positive": Incorrect — based on misreading the diff, looking at \
the wrong context, or claiming a problem that doesn't exist.
- "needs_human": Real but requires a product / design / security judgment \
the user must make. Don't auto-fix.
- "explanation_only": Asks for an explanation or context rather than a \
code change. Reply, don't edit.
- "duplicate": Substantively duplicates another finding on the same PR \
(e.g., re-flag at a slightly different line after a fix elsewhere).
- "already_addressed": The concern is resolved elsewhere in the diff; \
the line just hasn't been auto-resolved on GitHub.

Also output:
- "severity_override": Your opinion on the right severity (critical / high \
/ medium / low / none). Can match or differ from the reviewer's label. "none" \
means below the threshold worth surfacing.
- "recommended_action": fix / reply / ignore / escalate.

Respond with strict JSON only, no preamble:
{"verdict": "<verdict>", "confidence": <0.0-1.0>, \
"severity_override": "<one of the five>", \
"recommended_action": "<one of fix/reply/ignore/escalate>", \
"reason": "<one sentence>"}\
"""


@dataclasses.dataclass(frozen=True)
class JudgeResult:
    verdict: str
    confidence: float
    severity_override: str
    recommended_action: str
    reason: str
    status: str = "ok"  # "ok" | "skipped" | "error"
    skip_reason: str | None = None
    raw_response: str | None = None


class JudgeError(RuntimeError):
    pass


def prefs_path() -> Path:
    """Return the path to the per-user preferences file.

    Overridable via ``GGRL_STATE_DIR`` to share a directory with the existing
    sticky-receipt state file. Defaults to ``~/.config/gh-review-loop/``.
    """
    return loop_state.state_dir() / "preferences.json"


def _write_prefs(path: Path, prefs: dict[str, t.Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_preferences() -> dict[str, t.Any]:
    """Return saved preferences, initialising the file on first use if absent.

    Writing defaults on first load means the file is always present after the
    first script invocation — regardless of whether the user ever interacts
    with judge eval — so ``max_rereview_requests`` and other settings are
    discoverable and editable without a separate setup step.

    Default judge_mode is OFF for privacy: nothing is sent to OpenAI unless
    the user has explicitly opted in.
    """
    path = prefs_path()
    if not path.exists():
        prefs = _default_prefs()
        try:
            _write_prefs(path, prefs)
        except OSError:
            pass
        return prefs
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_prefs()
    if not isinstance(data, dict):
        return _default_prefs()
    # Validate / coerce known fields. Unknown fields ignored. Unknown
    # schema_version: keep mode if valid, otherwise fall back to off.
    mode = data.get("judge_mode")
    if mode not in VALID_JUDGE_MODES:
        mode = "off"
    max_rereview_requests = _coerce_max_rereview_requests(
        data.get("max_rereview_requests")
    )
    raw_profiles = data.get("profiles")
    profiles = raw_profiles if isinstance(raw_profiles, dict) else {}
    return {
        "schema_version": data.get("schema_version", PREFS_SCHEMA_VERSION),
        "judge_mode": mode,
        "judge_model": data.get("judge_model") or DEFAULT_MODEL,
        "judge_tip_shown": bool(data.get("judge_tip_shown", False)),
        "max_rereview_requests": max_rereview_requests,
        "profiles": profiles,
        "set_at": data.get("set_at") or "",
    }


def save_preferences(judge_mode: str, *, judge_model: str | None = None) -> dict[str, t.Any]:
    """Persist the user's judge preference. Returns the saved dict."""
    if judge_mode not in VALID_JUDGE_MODES:
        raise ValueError(f"judge_mode must be one of {VALID_JUDGE_MODES}; got {judge_mode!r}.")
    existing = load_preferences()
    prefs = {
        "schema_version": PREFS_SCHEMA_VERSION,
        "judge_mode": judge_mode,
        "judge_model": judge_model or DEFAULT_MODEL,
        "judge_tip_shown": existing.get("judge_tip_shown", False),
        "max_rereview_requests": existing.get(
            "max_rereview_requests", DEFAULT_MAX_REREVIEW_REQUESTS
        ),
        "profiles": existing.get("profiles", {}),
        "set_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2, sort_keys=True), encoding="utf-8")
    return prefs


def mark_tip_shown() -> None:
    """Persist judge_tip_shown=true without changing other prefs."""
    existing = load_preferences()
    existing["judge_tip_shown"] = True
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")


PROFILE_SOURCES = frozenset({"confirmed", "customized", "skipped"})


def get_profile(repo: str) -> dict[str, t.Any] | None:
    """Return the saved verification profile for ``repo`` (``owner/repo``), or None.

    New writes use GitHub's canonical repository spelling, but profiles saved by
    older versions may still be keyed with caller-supplied casing. Prefer the
    exact key and then fall back case-insensitively so an upgrade never turns an
    existing profile into a false first run.
    """
    profiles = load_preferences().get("profiles", {})
    if not isinstance(profiles, dict):
        return None
    exact = profiles.get(repo)
    if isinstance(exact, dict):
        return exact
    repo_key = repo.casefold()
    for saved_repo, profile in profiles.items():
        if (
            isinstance(saved_repo, str)
            and saved_repo.casefold() == repo_key
            and isinstance(profile, dict)
        ):
            return profile
    return None


def save_profile(
    repo: str,
    *,
    source: str,
    checks: list[dict[str, t.Any]] | None = None,
    detected_stack: str | None = None,
    working_directory: str = ".",
    timeout_seconds: int = 300,
) -> dict[str, t.Any]:
    """Persist a per-repo verification profile, preserving all other prefs.

    ``source`` must be one of PROFILE_SOURCES. A ``skipped`` profile carries no
    ``checks`` (the loop falls back to ad-hoc verification but does not re-prompt).
    Returns the saved profile dict.
    """
    if source not in PROFILE_SOURCES:
        raise ValueError(
            f"source must be one of {sorted(PROFILE_SOURCES)}; got {source!r}."
        )
    prefs = load_preferences()
    prefs["schema_version"] = PREFS_SCHEMA_VERSION
    profile: dict[str, t.Any] = {
        "source": source,
        "updated_at": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    if detected_stack is not None:
        profile["detected_stack"] = detected_stack
    if source != "skipped":
        profile["checks"] = checks if checks is not None else []
        profile["working_directory"] = working_directory
        profile["timeout_seconds"] = timeout_seconds
    profiles = dict(prefs.get("profiles", {}))
    profiles[repo] = profile
    prefs["profiles"] = profiles
    _write_prefs(prefs_path(), prefs)
    return profile


def _default_prefs() -> dict[str, t.Any]:
    return {
        "schema_version": PREFS_SCHEMA_VERSION,
        "judge_mode": "off",
        "judge_model": DEFAULT_MODEL,
        "judge_tip_shown": False,
        "max_rereview_requests": DEFAULT_MAX_REREVIEW_REQUESTS,
        "profiles": {},
        "set_at": "",
    }


def _coerce_max_rereview_requests(value: t.Any) -> int:
    if isinstance(value, bool):
        return DEFAULT_MAX_REREVIEW_REQUESTS
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return DEFAULT_MAX_REREVIEW_REQUESTS


def should_judge_run(*, mode: str, phase: str | None) -> bool:
    """Decide whether the judge should execute for this invocation.

    The script is the single source of truth: this function captures the
    full logic so the agent doesn't have to replicate it.

    - ``mode == "off"``        → never.
    - ``mode == "once"``       → always run (caller's responsibility to gate
                                  on user intent for a one-shot run).
    - ``mode == "on_cycle"``   → run only when phase == "cycle".
    - ``mode == "on_complete"`` → run only when phase == "complete".
    """
    if mode == "off":
        return False
    if mode == "once":
        return True
    if mode == "on_cycle":
        return phase == "cycle"
    if mode == "on_complete":
        return phase == "complete"
    return False


def build_user_prompt(finding: dict) -> str:
    """Render a reviewer finding into the structured user prompt."""
    severity = finding.get("severity") or "unknown"
    path = finding.get("path") or "(unknown path)"
    line = finding.get("line") or "?"
    body = (finding.get("body") or "").strip()
    diff_hunk = (finding.get("diff_hunk") or "").strip()

    parts = [
        f"File: {path}:{line}",
        f"Reviewer severity: {severity}",
        "",
        "Reviewer's finding:",
        body,
    ]
    if diff_hunk:
        parts.extend(["", "Surrounding diff hunk:", "```", diff_hunk, "```"])
    return "\n".join(parts)


def looks_like_placeholder_key(key: str | None) -> bool:
    """Return True if ``key`` looks like a placeholder rather than a real key.

    Catches the common failure mode where ``OPENAI_API_KEY`` is set to a
    template string (``REPLACE_WITH_YOUR_KEY``, ``sk-...``, etc.) — usually
    via a copy-pasted ``settings.json`` ``env`` block — which produces an
    opaque OpenAI 401 with the placeholder echoed back.

    Returns False for None / empty / non-string so callers can distinguish
    "missing" from "placeholder" with separate error messages. The
    isinstance guard matters because ``settings.json`` (Claude Code env
    injection) can pass a boolean or integer through unchanged — without
    it, ``key.upper()`` would raise ``AttributeError`` and crash the
    doctor before it could report anything useful.

    When ``OPENAI_BASE_URL`` is set, the user is pointing the SDK at a
    non-OpenAI endpoint (Ollama, LiteLLM, LM Studio, an enterprise gateway,
    etc.) where keys legitimately don't follow the ``sk-...`` shape or
    minimum length. Skip the shape checks in that case so the doctor
    doesn't fight self-hosted setups. The explicit placeholder-marker
    check still runs — a literal ``REPLACE_WITH_YOUR_KEY`` is always wrong.
    """
    if not isinstance(key, str) or not key:
        return False
    upper = key.upper()
    for marker in _PLACEHOLDER_MARKERS:
        if marker.upper() in upper:
            return True
    if os.environ.get("OPENAI_BASE_URL"):
        return False
    if len(key) < _MIN_REAL_KEY_LEN:
        return True
    if not key.startswith("sk-"):
        return True
    return False


def skipped_result(reason: str) -> JudgeResult:
    """Build a structured skipped result so callers don't have to invent fields."""
    return JudgeResult(
        verdict="needs_human",
        confidence=0.0,
        severity_override="none",
        recommended_action="ignore",
        reason="",
        status="skipped",
        skip_reason=reason,
    )


class JudgeClient:
    """Calls OpenAI to label a single Gemini finding.

    ``call_fn`` is injected for tests so we never touch the network.
    Caller logic (in fetch_gemini_threads.py) MUST decide whether to invoke
    judge() based on saved preferences + phase; this class just performs the
    call when asked.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        call_fn: t.Callable[..., dict] | None = None,
        temperature: float = 0.0,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        base_url: str | None = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        # Tiered resolution: explicit arg > resolver (env / dotfile / OS keystore).
        # Resolver source is exposed via api_key_source for the doctor's "which
        # key am I using?" output.
        if api_key:
            self.api_key = api_key
            self.api_key_source = "explicit"
        else:
            self.api_key, self.api_key_source = _resolve_api_key()
        self._call_fn = call_fn
        self.temperature = temperature
        self.request_timeout = request_timeout
        self.base_url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")

    def is_ready(self) -> tuple[bool, str | None]:
        """Return (ready, skip_reason).

        Caller uses this to short-circuit gracefully: when not ready, emit a
        ``skipped_result(reason)`` instead of trying and crashing. Each
        reason is phrased as actionable advice so users don't have to guess.

        No SDK probe — the call uses stdlib urllib, which is always
        available on any Python that can run this script.
        """
        if self._call_fn is not None:
            return True, None
        if not self.api_key:
            return False, (
                "OPENAI_API_KEY not found in any source (CLI flag, env var, "
                "~/.config/gh-review-loop/.env, OS keystore). "
                "Run 'python3 "
                "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/key_resolver.py "
                "--set' to store one, or 'judge_doctor.py' for full setup guidance."
            )
        # Defensive type check: settings.json env-injection can pass a bool
        # or int through unchanged. Without this, the next string operation
        # downstream would AttributeError instead of producing a clear
        # "this is not a string" message.
        if not isinstance(self.api_key, str):
            return False, (
                f"OPENAI_API_KEY is not a string (type={type(self.api_key).__name__}, "
                f"source={self.api_key_source}). Edit ~/.claude/settings.json "
                "and quote the value, or unset and re-store via key_resolver.py --set."
            )
        if looks_like_placeholder_key(self.api_key):
            preview = self.api_key[:12] + "..." if len(self.api_key) > 12 else self.api_key
            return False, (
                f"OPENAI_API_KEY looks like a placeholder ({preview!r}) "
                f"(source: {self.api_key_source}). "
                "Real OpenAI keys start with 'sk-' and are ~50+ chars. "
                "Run key_resolver.py --set to store a real key in the OS keystore, "
                "or check ~/.claude/settings.json 'env' block for a stale "
                "REPLACE_WITH_YOUR_KEY entry. Run judge_doctor.py for full setup help."
            )
        return True, None

    def _openai_call(self, messages: list[dict]) -> dict:
        """POST chat.completions via stdlib urllib. Mirrors the SDK's request shape
        for response_format=json_object so the rest of the pipeline is unchanged.

        Any HTTP / network / parse failure surfaces as JudgeError; the runner
        catches that uniformly.
        """
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": self.temperature,
            }
        ).encode("utf-8")
        req = _urlrequest.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "gh-review-loop/judge (urllib)",
            },
        )
        try:
            with _urlrequest.urlopen(req, timeout=self.request_timeout) as resp:
                # errors="replace": invalid UTF-8 from a misbehaving proxy
                # would otherwise raise UnicodeDecodeError outside the
                # HTTPError/URLError try arms and surface as an unhandled
                # exception. Replacement chars degrade the JSON parse
                # cleanly into a structured JudgeError below.
                raw = resp.read().decode("utf-8", errors="replace")
        except _urlerror.HTTPError as exc:
            # Surface the API's error body — for 401 we want the user to
            # see "Incorrect API key provided" not a generic message.
            # Truncate at 300 chars: corporate proxies / Cloudflare return
            # multi-KB HTML error pages on 502/403/523 that would otherwise
            # flood the terminal and bury the actionable line.
            try:
                err_body = exc.read().decode("utf-8")
                if len(err_body) > 300:
                    err_body = err_body[:300] + "...(truncated)"
            except Exception:  # noqa: BLE001
                err_body = ""
            raise JudgeError(
                f"OpenAI API HTTP {exc.code}: {err_body or exc.reason}"
            ) from exc
        except _urlerror.URLError as exc:
            raise JudgeError(f"OpenAI API network error: {exc.reason}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JudgeError(f"OpenAI API returned non-JSON: {raw!r}") from exc
        choices = payload.get("choices") or []
        if not choices:
            raise JudgeError("OpenAI response returned no choices.")
        content = (choices[0].get("message") or {}).get("content")
        return {"content": content, "model": payload.get("model", self.model)}

    def judge(self, finding: dict) -> JudgeResult:
        """Call the judge once. Returns a JudgeResult with status='ok' or status='skipped'.

        Raises JudgeError on parse / response problems so the caller can
        decide whether to skip THIS finding or abort.
        """
        ready, skip_reason = self.is_ready()
        if not ready:
            return skipped_result(skip_reason or "judge not ready")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(finding)},
        ]
        call = self._call_fn or self._openai_call
        raw = call(messages)
        content = raw.get("content") if isinstance(raw, dict) else None
        if not content:
            raise JudgeError(f"Empty response from judge: {raw!r}")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise JudgeError(f"Judge response was not valid JSON: {content!r}") from exc
        if not isinstance(payload, dict):
            raise JudgeError(f"Judge response was not a JSON object: {content!r}")

        verdict = payload.get("verdict")
        if verdict not in VALID_VERDICTS:
            raise JudgeError(
                f"Judge returned invalid verdict {verdict!r}; expected one of {VALID_VERDICTS}."
            )

        sev = payload.get("severity_override") or "none"
        if sev not in VALID_SEVERITY_OVERRIDES:
            sev = "none"

        action = payload.get("recommended_action") or "ignore"
        if action not in VALID_RECOMMENDED_ACTIONS:
            action = "ignore"

        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise JudgeError(f"Judge confidence is not a number: {payload!r}") from exc
        confidence = max(0.0, min(1.0, confidence))

        reason_val = payload.get("reason")
        reason = str(reason_val).strip() if reason_val is not None else ""

        return JudgeResult(
            verdict=verdict,
            confidence=confidence,
            severity_override=sev,
            recommended_action=action,
            reason=reason,
            status="ok",
            raw_response=content,
        )

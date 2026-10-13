#!/usr/bin/env python3
"""Cluster Gemini findings by a deterministic pattern signature.

Pure, stdlib-only, no network. Distinct from finding_fingerprint() in
fetch_gemini_threads.py: that keeps path + text to identify ONE finding;
this strips everything location- and instance-specific to capture the KIND
of suggestion, so two findings of the same kind in different files share a
signature.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sweep_siblings

# Local copies of trivial patterns so this module stays independent of the
# 2800-line fetch_gemini_threads module. Both reviewer conventions must be
# read here, mirroring thread_severity(): Gemini marks severity as image alt
# text (![high]); Codex as a P0-P3 badge image (![P1 Badge](...)). Reading
# only one leaves the other's clusters tied at "unknown", which silently
# collapses the Patterns block's severity ordering.
_SEVERITY_RE = re.compile(r"!\[(critical|high|medium|low)\]", re.IGNORECASE)
_CODEX_PRIORITY_RE = re.compile(r"!\[(P[0-3])(?:\s+Badge)?\]", re.IGNORECASE)
_PLAIN_PRIORITY_RE = re.compile(r"\A[ \t\r\n]*\[(P[0-3])\](?=\s|$)", re.IGNORECASE)
_CODEX_PRIORITY_TO_SEVERITY = {"p0": "critical", "p1": "high", "p2": "medium", "p3": "low"}
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}

_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")          # ![alt](url) images
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)       # ```suggestion``` etc.
_INLINE_CODE_RE = re.compile(r"`[^`]*`")               # `identifier`
_LINE_ECHO_RE = re.compile(r"\b(?:lines?|cols?|columns?)\s*\d+", re.IGNORECASE)
_COLON_NUM_RE = re.compile(r":\d+\b")                  # :204
# Opening quote must NOT be mid-word (no preceding letter), so possessives and
# contractions like source's / isn't are left intact while real string literals
# such as 'envs/prod' are still stripped.
_QUOTED_RE = re.compile(r"(?<![A-Za-z])(['\"]).*?\1")  # 'literal' / "literal"
_NUM_RE = re.compile(r"\b\d+\b")
# Also strips the English article "a"; an acceptable symmetric loss for v1 since
# it lets single-letter variable names cluster regardless of context.
_SINGLE_CHAR_VAR_RE = re.compile(r"\b[a-zA-Z]\b")  # bare single-letter identifiers
_WS_RE = re.compile(r"\s+")
# Inline HTML wrappers around reviewer markup (Codex wraps its priority badge
# in <sub><sub>…</sub></sub>). Stripped after _IMG_RE removes the image, so
# the empty tags don't leak into labels or hash into prose signatures.
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

# Gemini appends a <details>References</details> block of generic boilerplate
# that mentions every error type; it must be stripped before concept extraction
# or it poisons the classifier (merges unrelated findings).
_DETAILS_RE = re.compile(r"<details>.*?</details>", re.DOTALL | re.IGNORECASE)

# Intent-category classifier (pattern_signature v1).
#
# LANGUAGE CAVEAT: this vocabulary is Python-specific (OSError, ValueError, dict,
# list, AttributeError, TypeError, lstrip, lru_cache, ...). On non-Python repos
# Gemini uses different terms, no concept matches, and findings fall back to a
# unique prose hash → no clustering (degrades to per-finding, never mis-clusters).
# The planned language-agnostic prose-similarity replacement is tracked in #48.
_CONCEPT_KEYWORDS: dict[str, list[str]] = {
    "utf8":       ["errors='replace'", 'errors="replace"', "invalid utf-8", "utf-8 bytes", "unicodedecodeerror"],
    "valueerror": ["valueerror", "json.jsondecodeerror"],
    "oserror":    ["oserror", "filenotfounderror"],
    "tryexcept":  ["try-except", "try...except", "try except", "wrap "],
    "dict":       ["dictionary", "is not a dict", "not a dict"],
    "list":       ["is not a list", "not a list", "not list"],
    "attr":       ["attributeerror"],
    "typeerr":    ["typeerror"],
    "failfast":   ["fail fast", "cannot be resolved", "fails fast"],
    "perf":       ["lru_cache", "inefficient"],
    "tab":        ["lstrip", "leading tab", "tab character"],
    "codeowners": ["codeowners", "inline comment"],
    "yaml":       ["inline maps", "scalars", "quoted string"],
}

# The categories pattern_signature can assign. A cluster whose signature is one
# of these uses the category name as its human label.
KNOWN_CATEGORIES = {
    "tab-detection",
    "yaml-scalar-parse",
    "codeowners-parse",
    "perf-cache",
    "fail-fast-validation",
    "type-guard",
    "io-decode-guard",
    "exception-wrap",
}


def _clean_for_concepts(body: str) -> str:
    """Lowercased body with code, References boilerplate, and images removed."""
    body = _FENCED_RE.sub(" ", body)
    body = _DETAILS_RE.sub(" ", body)
    body = _IMG_RE.sub(" ", body)
    return body.lower()


def _concepts(cleaned: str) -> set[str]:
    return {c for c, kws in _CONCEPT_KEYWORDS.items() if any(k in cleaned for k in kws)}


def _categorize(cs: set[str]) -> str:
    """Assign ONE primary category by priority (first match wins).

    Single assignment (rather than overlap-graph membership) prevents a finding
    that mentions several concepts from chaining two distinct families into one
    cluster. Returns '' when no recognized concept is present.
    """
    if "tab" in cs:
        return "tab-detection"
    if "yaml" in cs:
        return "yaml-scalar-parse"
    if "codeowners" in cs:
        return "codeowners-parse"
    if "perf" in cs:
        return "perf-cache"
    if "failfast" in cs:
        return "fail-fast-validation"
    if ("dict" in cs or "list" in cs) and ("attr" in cs or "typeerr" in cs):
        return "type-guard"
    if "utf8" in cs or ("oserror" in cs and "valueerror" in cs):
        return "io-decode-guard"
    if "oserror" in cs or "tryexcept" in cs:
        return "exception-wrap"
    return ""


def _first_body(thread: Any) -> str:
    if not isinstance(thread, dict):
        return ""
    comments = thread.get("comments")
    if isinstance(comments, dict):
        comments = comments.get("nodes")
    if not isinstance(comments, list) or not comments:
        return ""
    first = comments[0]
    body = first.get("body") if isinstance(first, dict) else None
    return body if isinstance(body, str) else ""


def _normalize(body: str) -> str:
    body = _PLAIN_PRIORITY_RE.sub(" ", body, count=1)
    body = _IMG_RE.sub(" ", body)
    body = _HTML_TAG_RE.sub(" ", body)
    body = _FENCED_RE.sub(" ", body)
    body = _INLINE_CODE_RE.sub(" ", body)
    body = _LINE_ECHO_RE.sub(" ", body)
    body = _COLON_NUM_RE.sub(" ", body)
    body = _QUOTED_RE.sub(" ", body)
    body = _NUM_RE.sub(" ", body)
    body = _SINGLE_CHAR_VAR_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip().lower()
    return body


def pattern_signature(thread: Any) -> str:
    """Stable signature of a finding's KIND. '' for malformed input.

    Two-stage: classify into an intent category from the concept vocabulary
    (the category name IS the signature — stable and human-readable); if no
    concept is recognized, fall back to a unique prose hash so unrelated
    findings each stay their own group (never merged).
    """
    body = _first_body(thread)
    if not body:
        return ""
    category = _categorize(_concepts(_clean_for_concepts(body)))
    if category:
        return category
    normalized = _normalize(body)
    if not normalized:
        return ""
    return hashlib.sha1(normalized[:1000].encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Cluster:
    signature: str
    label: str
    severity: str
    sites: tuple[str, ...]  # immutable, to honor frozen=True
    count: int  # invariant: count == len(sites)


def _severity(thread: Any) -> str:
    """Match thread_severity's selection of the first marked comment."""
    if not isinstance(thread, dict):
        return "unknown"
    comments = thread.get("comments")
    if isinstance(comments, dict):
        comments = comments.get("nodes")
    if not isinstance(comments, list):
        return "unknown"
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        match = _SEVERITY_RE.search(body)
        if match:
            return match.group(1).lower()
        codex_match = _CODEX_PRIORITY_RE.search(body)
        if codex_match:
            return _CODEX_PRIORITY_TO_SEVERITY[codex_match.group(1).lower()]
        plain_match = _PLAIN_PRIORITY_RE.match(body)
        if plain_match:
            return _CODEX_PRIORITY_TO_SEVERITY[plain_match.group(1).lower()]
    return "unknown"


def _site(thread: dict[str, Any]) -> str:
    path = thread.get("path") or "?"
    line = thread.get("line")
    if line is not None:
        start = thread.get("startLine")
    else:
        line = thread.get("originalLine")
        start = thread.get("originalStartLine")
    if start is not None and line is not None and start < line:
        return f"{path}:{start}-{line}"
    return f"{path}:{line}" if line is not None else str(path)


def _label(body: str) -> str:
    """Short human title: the normalized prose, truncated. Not polished."""
    norm = _normalize(body)
    return (norm[:60] + "…") if len(norm) > 60 else norm


# Two findings shaped the same must share at least this many tokens at their
# anchored source lines before prose-distinct clusters are merged. Same floor as
# the sweep's own specificity check, for the same reason: fewer than two shared
# tokens is not a shape.
MIN_SHAPE_TOKENS = 2

# Local placeholder names recur across unrelated multi-line spans. They remain
# matching constraints, but cannot qualify two spans as the same code shape.
_GENERIC_SHAPE_TOKENS = frozenset({
    "item", "items", "member", "members", "name", "names",
    "term", "terms", "value", "values",
})

# Merging is capped so one over-broad shape cannot swallow an entire review into
# a single cluster. Beyond this, the findings are more likely diverse than alike.
MAX_SHAPE_GROUP = 12


def _anchored_tokens(thread: Any, root: Path) -> set[str]:
    """Return tokens from a finding's anchored code span, or an empty set."""
    if not isinstance(thread, dict):
        return set()
    path = thread.get("path")
    end = thread.get("line")
    if end is not None:
        start = thread.get("startLine") or end
    else:
        end = thread.get("originalLine")
        start = thread.get("originalStartLine") or end
    if (
        not isinstance(path, str)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 1
        or end < start
    ):
        return set()
    # Same containment rule as the sweep: the path comes from a PR thread,
    # which is untrusted. A symlink or ../ escape yields no tokens rather than
    # tokens from a file the diff never touched.
    target = sweep_siblings._contained_path(root, path)
    if target is None:
        return set()
    try:
        if target.stat().st_size > sweep_siblings.MAX_FILE_BYTES:
            return set()
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return set()
    if end > len(lines):
        return set()
    tokens: set[str] = set()
    for text in lines[start - 1:end]:
        if sweep_siblings.is_code_line(text):
            tokens.update(sweep_siblings.tokenize(text))
    return tokens


def shape_groups(threads: list[Any], root: Path) -> list[list[int]]:
    """Group thread indices whose anchored source spans share a code shape.

    Prose clustering fails when a reviewer describes the same defect two ways --
    "exception-wrap" and "add error checks around the parsed JSON" hash apart,
    so a pattern present at two sites looks like two patterns at one site each
    and never reaches the sweep's two-site minimum.

    The code does not vary the way the prose does. Grouping on the anchored
    spans' shared tokens recovers the pattern regardless of what the bot called
    it. Greedy over a stable order, so the result does not depend on the order
    threads arrive in.
    """
    tokens = [_anchored_tokens(thread, root) for thread in threads]
    order = sorted(range(len(threads)), key=lambda i: (_site(threads[i]) if isinstance(threads[i], dict) else str(i), i))

    groups: list[dict[str, Any]] = []
    for index in order:
        current = tokens[index]
        if not current:
            continue
        best: dict[str, Any] | None = None
        best_common: set[str] = set()
        for group in groups:
            if len(group["members"]) >= MAX_SHAPE_GROUP:
                continue
            common = group["tokens"] & current
            significant_count = sum(
                sweep_siblings.is_significant_token(token)
                and token not in _GENERIC_SHAPE_TOKENS
                for token in common
            )
            if significant_count >= MIN_SHAPE_TOKENS and len(common) > len(best_common):
                best, best_common = group, common
        if best is None:
            groups.append({"tokens": set(current), "members": [index]})
        else:
            best["tokens"] = best_common
            best["members"].append(index)
    return [sorted(g["members"]) for g in groups if len(g["members"]) > 1]


def cluster(threads: list[Any], *, root: str | Path | None = None) -> list[Cluster]:
    """Group threads by pattern_signature; sort by severity desc then count desc.

    With ``root`` set, clusters whose findings are anchored to lines of the same
    code shape are merged afterwards, so a pattern the reviewer described two
    different ways still reaches the sweep as one pattern. Without ``root`` the
    behaviour is prose-only, exactly as before.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for thread in threads:
        sig = pattern_signature(thread)
        if not sig:
            continue
        groups.setdefault(sig, []).append(thread)

    if root is not None:
        groups = _merge_by_shape(groups, Path(root))

    clusters: list[Cluster] = []
    for sig, members in groups.items():
        best = min(members, key=lambda t: _SEVERITY_ORDER[_severity(t)])
        severity = _severity(best)
        # A recognized category is its own clean label; otherwise fall back to a
        # truncated prose fragment from the highest-severity member.
        label = sig if sig in KNOWN_CATEGORIES else _label(_first_body(best))
        clusters.append(
            Cluster(
                signature=sig,
                label=label,
                severity=severity,
                sites=tuple(_site(m) for m in members),
                count=len(members),
            )
        )
    clusters.sort(key=lambda c: (_SEVERITY_ORDER[c.severity], -c.count))
    return clusters


def _merge_by_shape(
    groups: dict[str, list[dict[str, Any]]], root: Path
) -> dict[str, list[dict[str, Any]]]:
    """Merge prose-distinct clusters whose findings share a code shape."""
    keys = list(groups)
    representative = [groups[k][0] for k in keys]
    merged_indices = shape_groups(representative, root)
    if not merged_indices:
        return groups

    absorbed: set[int] = set()
    out: dict[str, list[dict[str, Any]]] = {}
    for indices in merged_indices:
        members: list[dict[str, Any]] = []
        for i in indices:
            members.extend(groups[keys[i]])
            absorbed.add(i)
        # A shape-derived signature is its own kind, and says so in the receipt:
        # a reader seeing `shape:` knows the merge came from the code, not the
        # reviewer's wording.
        joined = "|".join(sorted(keys[i] for i in indices))
        out["shape:" + hashlib.sha1(joined.encode()).hexdigest()[:8]] = members
    for i, key in enumerate(keys):
        if i not in absorbed:
            out[key] = groups[key]
    return out


def recurrence_stats(
    current_sigs: list[str],
    *,
    prior_sigs: set[str],
    swept_sigs: set[str],
) -> dict[str, Any]:
    """Convergence signals for one cycle.

    - distinct_patterns: number of unique signatures this cycle
    - recurrence_rate: fraction of this cycle's findings whose signature was
      seen in a prior cycle (0.0 when there are no findings)
    - recurred_after_sweep: sorted signatures swept in a PRIOR cycle that show
      up again now. Membership in prior_sigs is required, so a pattern marked
      swept and still present in the SAME cycle (its fix not yet re-reviewed)
      does not false-alarm — a sweep only "fails" once a later review re-flags it.
    """
    valid = [s for s in current_sigs if s]
    total = len(valid)
    recurred = sum(1 for s in valid if s in prior_sigs)
    recurred_after_sweep = sorted({s for s in valid if s in swept_sigs and s in prior_sigs})
    return {
        "distinct_patterns": len(set(valid)),
        "recurrence_rate": (recurred / total) if total else 0.0,
        "recurred_after_sweep": recurred_after_sweep,
    }

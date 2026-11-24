#!/usr/bin/env python3
"""Deterministic replay of the sibling sweep over captured reviewer payloads.

Runs the real clusterer and the real sweep against reviewer findings captured
from PR #67 (Sourcery) and PR #129 (Codex), plus the reviewed source vendored
beside them. No network, no
arguments, stdlib only — the output depends on nothing but this repository.

    python3 evals/replay/replay.py

Each fixture is clustered twice: once without a root (prose only, the pre-#69
behaviour) and once with the fixture's source directory as root (the shipping
behaviour, which also merges clusters whose findings anchor to the same code
shape). Any cluster reaching two sites is then swept for unflagged siblings.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SCRIPTS = REPO_ROOT / "plugins/gh-review-loop/skills/gh-review-loop/scripts"
sys.path.insert(0, str(SCRIPTS))

import cluster_findings  # noqa: E402
import metrics  # noqa: E402
import sweep_siblings  # noqa: E402

FIXTURES = HERE / "fixtures"
RUNS = ("run1", "run2", "codex")


def replay_one(name: str) -> None:
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    threads = payload["threads"]
    # Each capture replays against the source it was written about; the two
    # Sourcery runs share one tree, the Codex capture has its own.
    source_root = FIXTURES / payload.get("source_root", "src")

    print("=" * 72)
    print(f"{name}: {payload['description']}")
    print(f"commit: {payload['commit']}")
    print(f"threads: {len(threads)}")
    for thread in threads:
        line = thread.get("line")
        if line is None:
            line = thread.get("originalLine")
        print(f"  - {thread['path']}:{line}")
    print("=" * 72)

    # Prose only: what clustering saw before findings could be grouped by the
    # shape of the code they anchor to.
    prose = cluster_findings.cluster(threads)
    print("\n-- clustered by finding prose alone --")
    print(metrics.format_patterns_block(prose) or "Patterns (0):")

    # The shipping path: fetch_gemini_threads passes root=repo_root().
    shaped = cluster_findings.cluster(threads, root=source_root)
    print("\n-- clustered by prose and code shape (shipping behaviour) --")
    print(metrics.format_patterns_block(shaped) or "Patterns (0):")

    print("\n-- sweep --")
    swept_any = False
    for cluster in shaped:
        if cluster.count < sweep_siblings.MIN_FLAGGED_SITES:
            continue
        swept_any = True
        result = sweep_siblings.sweep(
            signature=cluster.signature,
            label=cluster.label,
            sites=list(cluster.sites),
            changed_files=payload["changed_files"],
            root=source_root,
        )
        print(sweep_siblings.render_report(result))
    if not swept_any:
        print(
            f"no cluster reached {sweep_siblings.MIN_FLAGGED_SITES} sites; "
            "nothing to sweep"
        )
    print()


def main() -> int:
    for name in RUNS:
        replay_one(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

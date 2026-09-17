import subprocess

import pytest
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "gh-review-loop" / "skills" / "gh-review-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def _forbid_real_gh(monkeypatch):
    """Fail loudly if a test shells out to `gh`.

    The suite is hermetic by design, and a test that reaches the network is a
    bug that only shows up on a maintainer's authenticated machine -- where it
    looks like a mystery failure in unrelated code, not like a missing stub.
    Raising here turns that into a named assertion at the call site.

    Tests that exercise gh-invoking code pass an explicit ``runner``; tests that
    exercise the surrounding logic must stub the helper they don't care about.
    """
    real_run = subprocess.run

    def guard(cmd, *args, **kwargs):
        argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        first = str(argv[0]) if argv else ""
        if first == "gh" or first.endswith("/gh"):
            raise AssertionError(
                "hermetic test suite: a test invoked the real `gh` "
                f"({list(argv)[:4]}). Pass runner=... or stub the helper."
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guard)


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path_factory, monkeypatch):
    """Point per-user state at a temp directory for every test.

    Several scripts resolve their state and preferences under GGRL_STATE_DIR,
    falling back to the real state dir. load_preferences() creates
    that file when absent, so any test reaching it writes to the developer's
    real config. Tests that need a specific directory still set the variable
    themselves and override this.
    """
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path_factory.mktemp("ggrl-state")))


def stub_cap_check_known_under_cap(monkeypatch):
    """Neutralize request_rereview's cap check for tests that are not about it.

    The cap needs the authenticated login and the PR's comments, both from
    `gh`. Since an unknown count now refuses to post (an uncountable write is
    an uncapped write), "inert" means a *known* count of zero, not an unknown
    one. Unit tests of the counter pass an explicit runner and still reach the
    real function.
    """
    import request_rereview

    real_count = request_rereview.count_agent_pings

    def count_or_zero(repo, pr, trigger, agent_login, runner=None):
        if runner is None:
            return 0
        return real_count(repo, pr, trigger, agent_login, runner=runner)

    monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
    monkeypatch.setattr(request_rereview, "count_agent_pings", count_or_zero)

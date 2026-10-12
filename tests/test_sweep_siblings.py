"""Tests for the sibling sweep.

The sweep proposes edits to code the reviewer never mentioned, so the tests
that matter most are the ones asserting it stays quiet: too few sites, a
too-generic pattern, a file outside the diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sweep_siblings as sweeper


MIRROR_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "mirror_corpus.json").read_text()
)


UTF8 = '''\
def load_config(path):
    return path.read_text().encode()


def load_manifest(path):
    return path.read_text().encode()


def load_lockfile(path):
    return path.read_text().encode()


def unrelated(value):
    return value * 2
'''

SALT = '''\
def hash_password(password, salt):
    return digest(password, salt.encode())
'''


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.py").write_text(UTF8)
    (tmp_path / "app" / "auth.py").write_text(SALT)
    (tmp_path / "vendor.py").write_text(UTF8)
    return tmp_path


class TestTokenize:
    def test_a_line_tokenizes_the_same_whatever_its_literals_are(self):
        """Literals vary site to site; the pattern is what is left."""
        assert (sweeper.tokenize("write_file('a.json', 42)")
                == sweeper.tokenize("write_file('totally-different.yaml', 9999)"))

    def test_literals_do_not_leak_into_tokens(self):
        tokens = sweeper.tokenize("greet('hello world', 1234)")
        assert "hello" not in tokens
        assert "1234" not in tokens
        assert "greet" in tokens

    def test_drops_stopwords_and_single_characters(self):
        tokens = sweeper.tokenize("if self.x == y: return z")
        assert "self" not in tokens
        assert "if" not in tokens
        assert "return" not in tokens
        assert "x" not in tokens

    def test_is_case_insensitive(self):
        assert sweeper.tokenize("Path.read_text()") == sweeper.tokenize("path.READ_TEXT()")

    @pytest.mark.parametrize("token", ["name", "snake_case", "x1", "A"])
    def test_letter_tokens_are_significant(self, token):
        assert sweeper.is_significant_token(token)

    @pytest.mark.parametrize("token", ["!", "&&", "=>", ");"])
    def test_punctuation_tokens_are_not_significant(self, token):
        assert not sweeper.is_significant_token(token)




class TestInvariantTokens:
    def test_keeps_only_what_every_site_shares(self):
        common = sweeper.invariant_tokens([
            "value = payload.read_text().encode()",
            "other = handle.read_text().encode()",
        ])
        assert "read_text" in common
        assert "encode" in common
        # Present at one site only, so it describes that site, not the pattern.
        assert "payload" not in common
        assert "handle" not in common

    def test_empty_input_yields_nothing(self):
        assert sweeper.invariant_tokens([]) == set()
        assert sweeper.invariant_tokens(["", "   "]) == set()


class TestParseSite:
    @pytest.mark.parametrize("raw,path,line", [
        ("app/config.py:12", "app/config.py", 12),
        ("app/config.py", "app/config.py", None),
        ("C:/src/app.py:3", "C:/src/app.py", 3),
        ("weird:name.py", "weird:name.py", None),
        ("", "", None),
    ])
    def test_splits_on_a_trailing_numeric_line(self, raw, path, line):
        site = sweeper.parse_site(raw)
        assert (site.path, site.line) == (path, line)

    def test_parses_a_flagged_range(self):
        site = sweeper.parse_site("lib/options.ts:5-8")

        assert site.path == "lib/options.ts"
        assert site.start_line == 5
        assert site.line == 8
        assert str(site) == "lib/options.ts:5-8"

    def test_a_range_starting_below_line_one_is_not_a_range(self):
        """Line numbers are 1-based. A 0 start would make ``start_line or line``
        fall through to the end line, narrowing the self-overlap guard until a
        seed reports its own location as a mirror of itself."""
        site = sweeper.parse_site("lib/options.ts:0-8")

        assert site.start_line is None
        assert site.line is None
        assert site.path == "lib/options.ts:0-8"

    def test_a_malformed_zero_start_cannot_self_mirror(self, tmp_path):
        (tmp_path / "f.py").write_text(
            "# leading note\n"
            "alpha_step()\nbeta_step()\ngamma_step()\n"
            "# trailing note\n"
            "unrelated()\n"
            "alpha_step()\nbeta_step()\ngamma_step()\n"
        )

        result = sweeper.sweep(
            signature="zero", label="zero",
            sites=["f.py:0-5"], changed_files=["f.py"], root=tmp_path,
        )

        assert "f.py:2-4" not in [c["site"] for c in result.candidates]


@pytest.fixture
def mirror_repo(tmp_path):
    for fixture in MIRROR_CORPUS:
        target = tmp_path / fixture["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        padding = [""] * (fixture["startLine"] - 1)
        target.write_text("\n".join(padding + fixture["lines"]) + "\n")
    return tmp_path


class TestSweep:
    def _sweep(self, repo, **overrides):
        kwargs = dict(
            signature="utf8",
            label="missing decode guard",
            sites=["app/config.py:2", "app/config.py:6"],
            changed_files=["app/config.py", "app/auth.py"],
            root=repo,
        )
        kwargs.update(overrides)
        return sweeper.sweep(**kwargs)

    def test_finds_the_sibling_the_reviewer_missed(self, repo):
        result = self._sweep(repo)
        assert result.status == "ok"
        assert [c["site"] for c in result.candidates] == ["app/config.py:10"]
        assert "encode" in result.invariant_tokens

    def test_does_not_re_report_the_flagged_sites(self, repo):
        result = self._sweep(repo)
        flagged = {"app/config.py:2", "app/config.py:6"}
        assert flagged.isdisjoint({c["site"] for c in result.candidates})

    def test_token_sweep_does_not_report_lines_inside_flagged_ranges(self, tmp_path):
        (tmp_path / "ranges.py").write_text(
            "one = path.read_text().encode()\n"
            "two = path.read_text().encode()\n"
            "unrelated()\n"
            "three = path.read_text().encode()\n"
            "four = path.read_text().encode()\n"
        )

        result = sweeper.sweep(
            signature="range-shape",
            label="range shape",
            sites=["ranges.py:1-2", "ranges.py:4-5"],
            changed_files=["ranges.py"],
            root=tmp_path,
        )

        assert result.candidates == []

    def test_never_leaves_the_changed_file_set(self, repo):
        """vendor.py contains the identical pattern but is not in the diff."""
        result = self._sweep(repo)
        assert all(not c["path"].startswith("vendor") for c in result.candidates)

        widened = self._sweep(repo, changed_files=["app/config.py", "vendor.py"])
        assert any(c["path"] == "vendor.py" for c in widened.candidates)

    def test_does_not_read_a_flagged_site_outside_changed_files(self, repo, monkeypatch):
        original = sweeper._read_lines
        reads = []

        def recording_reader(path):
            reads.append(path)
            return original(path)

        monkeypatch.setattr(sweeper, "_read_lines", recording_reader)
        result = self._sweep(
            repo,
            sites=["vendor.py:2", "vendor.py:6"],
            changed_files=["app/config.py"],
        )

        assert result.status == "too_few_sites"
        assert all(path.name != "vendor.py" for path in reads)

    def test_a_single_flagged_site_is_not_a_pattern(self, repo):
        result = self._sweep(repo, sites=["app/config.py:2"])
        assert result.status == "too_few_sites"
        assert result.candidates == []

    def test_refuses_when_the_shared_shape_is_too_generic(self, repo):
        """Two sites sharing almost nothing must not sweep the whole diff."""
        (repo / "app" / "thin.py").write_text("alpha(x)\nbravo(y)\ncharlie(z)\n")
        result = self._sweep(
            repo,
            sites=["app/thin.py:1", "app/thin.py:2"],
            changed_files=["app/thin.py"],
        )
        assert result.status == "pattern_too_thin"
        assert result.candidates == []
        assert "Too broad to sweep safely" in result.reason

    def test_punctuation_only_intersection_is_too_thin(self, tmp_path):
        (tmp_path / "conditions.js").write_text(
            "!alpha && beta\n"
            "!gamma && delta\n"
        )
        result = sweeper.sweep(
            signature="conditional-gate",
            label="independent conditional gate",
            sites=[
                "conditions.js:1",
                "conditions.js:2",
            ],
            changed_files=["conditions.js"],
            root=tmp_path,
        )

        assert result.status == "pattern_too_thin"
        assert result.invariant_tokens == ("!", "&&")
        assert result.candidates == []

    def test_flagged_range_finds_the_twin_implementation(self, mirror_repo):
        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["lib/options.ts:5-8"],
            changed_files=[fixture["path"] for fixture in MIRROR_CORPUS],
            root=mirror_repo,
        )

        assert result.status == "ok"
        assert len(result.candidates) == 1
        assert result.candidates[0]["candidateClass"] == "mirror"
        assert result.candidates[0]["site"] == (
            "src/options.js:12-15"
        )
        assert result.candidates[0]["fingerprint"]

    def test_mirror_rejects_a_structural_only_seed(self, tmp_path):
        (tmp_path / "generic.js").write_text(
            "return;\n}\n\nreturn;\n}\n"
        )

        result = sweeper.sweep(
            signature="generic-ending",
            label="generic ending",
            sites=["generic.js:1-2"],
            changed_files=["generic.js"],
            root=tmp_path,
        )

        assert result.status == "too_few_sites"
        assert result.candidates == []

    def test_mirror_never_searches_outside_changed_files(self, mirror_repo):
        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["lib/options.ts:5-8"],
            changed_files=["lib/options.ts"],
            root=mirror_repo,
        )

        assert result.status == "too_few_sites"
        assert result.candidates == []

    def test_mirror_does_not_re_report_another_flagged_range(self, mirror_repo):
        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=[
                "lib/options.ts:5-8",
                "src/options.js:12-15",
            ],
            changed_files=[fixture["path"] for fixture in MIRROR_CORPUS],
            root=mirror_repo,
        )

        assert all(
            candidate["candidateClass"] != "mirror"
            for candidate in result.candidates
        )

    def test_mirror_does_not_re_report_a_flagged_single_line(self, tmp_path):
        block = (
            "const normalized = rawOptions\n"
            "  .filter((option) => option.enabled)\n"
            "  .map((option) => option.name.trim())\n"
            "  .sort();\n"
        )
        (tmp_path / "source.js").write_text(block)
        (tmp_path / "twin.js").write_text(block)

        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["source.js:1-4", "twin.js:2"],
            changed_files=["source.js", "twin.js"],
            root=tmp_path,
        )

        assert all(candidate["candidateClass"] != "mirror" for candidate in result.candidates)

    @pytest.mark.parametrize(
        ("path", "source"),
        [
            (
                "template.js",
                "message = `header\n  // keep this\n  value`;\n",
            ),
        ],
    )
    def test_mirror_seed_inside_multiline_literal_uses_file_context(
        self, tmp_path, path, source
    ):
        (tmp_path / path).write_text(source)
        twin = tmp_path / f"twin{Path(path).suffix}"
        twin.write_text(source)

        result = sweeper.sweep(
            signature="literal-body",
            label="literal body",
            sites=[f"{path}:2-3"],
            changed_files=[path, twin.name],
            root=tmp_path,
        )

        assert [candidate["site"] for candidate in result.candidates] == [
            f"{twin.name}:2-3"
        ]

    def test_a_hash_inside_a_python_docstring_is_string_data(self, tmp_path):
        """Staged within one file, because normalized matching does not cross
        files. Were the `#` line treated as a comment it would be dropped, the
        seed would fall under the two-line floor, and nothing would match --
        so finding the second copy is what proves it was kept."""
        literal = 'message = """header\n  # keep this\n  value"""\n'
        (tmp_path / "template.py").write_text(literal + "spacer()\n" + literal)

        result = sweeper.sweep(
            signature="literal-body",
            label="literal body",
            sites=["template.py:2-3"],
            changed_files=["template.py"],
            root=tmp_path,
        )

        assert [c["site"] for c in result.candidates] == ["template.py:6-7"]

        # The block is byte-identical, so `exact` is the stronger and honest
        # label. What proves the `#` was read as string data is the tag on it:
        # a comment would have been stripped from the normalized index, and
        # both indexes agree it is string content.
        normalized, verbatim = sweeper._python_block_lines(
            (tmp_path / "template.py").read_text().splitlines()
        )
        assert (2, "  # keep this", "string") in normalized
        assert (2, "  # keep this", "string") in verbatim

    def test_mirror_does_not_re_report_seed_after_comment_stripping(self, tmp_path):
        """Only normalized mode drops lines, so only there can the reported
        range drift off the seed's own line numbers. Raw mode keeps every line
        and cannot shift at all."""
        (tmp_path / "query.py").write_text(
            "# flagged range includes this comment\n"
            "normalized_options = [\n"
            "    option.name.strip()\n"
            "    for option in raw_options\n"
            "]\n"
            "\n"
            "normalized_options = [\n"
            "    option.name.strip()\n"
            "    for option in raw_options\n"
            "]\n"
        )

        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["query.py:1-5"],
            changed_files=["query.py"],
            root=tmp_path,
        )

        assert [candidate["site"] for candidate in result.candidates] == [
            "query.py:7-10"
        ]

    def test_mirror_does_not_report_windows_overlapping_the_seed(self, tmp_path):
        (tmp_path / "repeat.js").write_text("x();\n" * 5)

        result = sweeper.sweep(
            signature="repeated-block",
            label="repeated block",
            sites=["repeat.js:2-4"],
            changed_files=["repeat.js"],
            root=tmp_path,
        )

        assert result.candidates == []

    def test_token_candidates_are_distinct_from_mirrors(self, repo):
        result = self._sweep(repo)

        assert {candidate["candidateClass"] for candidate in result.candidates} == {"token"}

    def test_token_hit_inside_a_mirror_is_not_double_counted(self, tmp_path):
        block = (
            "prepare();\n"
            "value = path.read_text().encode();\n"
        )
        (tmp_path / "copies.js").write_text(
            block + "unrelated();\n" + block + "unrelated();\n" + block
        )

        result = sweeper.sweep(
            signature="copied-block",
            label="copied block",
            sites=["copies.js:1-2", "copies.js:4-5"],
            changed_files=["copies.js"],
            root=tmp_path,
        )

        assert [(candidate["candidateClass"], candidate["site"])
                for candidate in result.candidates] == [
            ("mirror", "copies.js:7-8")
        ]

    def test_a_line_matching_only_some_invariant_tokens_is_not_a_sibling(self, repo):
        """Containment is all-or-nothing; partial overlap is not a match."""
        (repo / "app" / "partial.py").write_text(
            "a = path.read_text().encode()\n"
            "b = other.read_text().encode()\n"
            "c = thing.read_text()\n"          # read_text but no encode
            "d = thing.encode()\n"             # encode but no read_text
        )
        result = self._sweep(
            repo,
            sites=["app/partial.py:1", "app/partial.py:2"],
            changed_files=["app/partial.py"],
        )
        assert result.status == "ok"
        assert result.candidates == []

    def test_stops_when_the_flagged_lines_cannot_be_read(self, repo):
        result = self._sweep(
            repo,
            sites=["app/missing.py:2", "app/gone.py:4"],
            changed_files=["app/missing.py", "app/gone.py"],
        )
        assert result.status == "no_source"
        assert result.candidates == []

    def test_out_of_range_line_numbers_do_not_crash(self, repo):
        result = self._sweep(repo, sites=["app/config.py:9999", "app/config.py:2"])
        assert result.status in {"no_source", "too_few_sites", "pattern_too_thin", "ok"}

    def test_a_missing_changed_file_is_skipped_not_fatal(self, repo):
        result = self._sweep(repo, changed_files=["app/config.py", "app/deleted.py"])
        assert result.status == "ok"

    def test_candidates_are_capped_and_the_cap_is_reported(self, repo):
        result = self._sweep(repo, max_candidates=0)
        assert result.truncated is True
        assert result.candidates == []
        assert "refactor, not a sweep" in result.reason

    def test_candidates_are_ordered_by_file_then_line(self, repo):
        (repo / "app" / "extra.py").write_text(UTF8)
        result = self._sweep(
            repo, changed_files=["app/extra.py", "app/config.py"]
        )
        sites = [(c["path"], c["line"]) for c in result.candidates]
        assert sites == sorted(sites)

    def test_every_candidate_carries_its_evidence(self, repo):
        result = self._sweep(repo)
        for candidate in result.candidates:
            assert candidate["matchedTokens"]
            assert candidate["text"]
            assert set(candidate["matchedTokens"]) == set(result.invariant_tokens)

    def test_an_oversized_file_is_skipped(self, repo, monkeypatch):
        monkeypatch.setattr(sweeper, "MAX_FILE_BYTES", 1)
        result = self._sweep(repo)
        assert result.status == "no_source"

class TestRawMirrorMatching:
    """Default mode for every language: blocks match when their text is
    identical. No per-language knowledge, so there is no normalizer to get
    wrong -- which is what retires the whole class of collision findings."""

    @staticmethod
    def _sweep(tmp_path, sites, changed_files):
        return sweeper.sweep(
            signature="guard", label="guard",
            sites=sites, changed_files=changed_files, root=tmp_path,
        )

    HEREDOC_SCRIPT = (
        "write_config() {\n"
        "  cat <<EOF\n"
        "# PAYLOAD setting\n"
        "value=1\n"
        "EOF\n"
        "}\n"
    )

    @classmethod
    def _heredoc(cls, payload):
        return cls.HEREDOC_SCRIPT.replace("PAYLOAD", payload)

    # --- the collision findings, now retired by construction ----------------
    # Each of these was a reported false-positive under normalized matching.
    # Raw matching refuses them because the text simply differs.

    def test_heredoc_payload_differences_are_visible(self, tmp_path):
        (tmp_path / "script.sh").write_text(
            self._heredoc("alpha") + self._heredoc("beta")
        )
        assert self._sweep(tmp_path, ["script.sh:2-5"], ["script.sh"]).candidates == []

    def test_heredoc_payload_spacing_is_visible(self, tmp_path):
        (tmp_path / "spacing.sh").write_text(
            "cat <<EOF\nindent_one   two\nEOF\n"
            "cat <<EOF\nindent_one two\nEOF\n"
        )
        assert self._sweep(tmp_path, ["spacing.sh:1-3"], ["spacing.sh"]).candidates == []

    def test_heredoc_terminator_lookalikes_are_visible(self, tmp_path):
        block = "cat <<EOF\nalpha_data\nEOF   \n# PAYLOAD\nEOF\n"
        (tmp_path / "fake_end.sh").write_text(
            block.replace("PAYLOAD", "FIRST_DATA")
            + block.replace("PAYLOAD", "SECOND_DATA")
        )
        assert self._sweep(tmp_path, ["fake_end.sh:1-5"], ["fake_end.sh"]).candidates == []

    def test_non_identifier_heredoc_delimiters_need_no_parsing(self, tmp_path):
        block = "cat <<$EOF\n# PAYLOAD\nshared_body\n$EOF\n"
        (tmp_path / "dollar.sh").write_text(
            block.replace("PAYLOAD", "alpha_data")
            + block.replace("PAYLOAD", "beta_data")
        )
        assert self._sweep(tmp_path, ["dollar.sh:1-4"], ["dollar.sh"]).candidates == []

    def test_yaml_nesting_depth_is_visible(self, tmp_path):
        (tmp_path / "compose.yaml").write_text(
            "services:\n  web:\n    image: alpine_base\n    command: serve_http\n"
            "overrides:\n      image: alpine_base\n      command: serve_http\n"
        )
        assert self._sweep(
            tmp_path, ["compose.yaml:3-4"], ["compose.yaml"]
        ).candidates == []

    def test_yaml_block_scalar_payloads_are_visible(self, tmp_path):
        (tmp_path / "messages.yaml").write_text(
            "first:\n  message: |\n    # alpha_payload\n    body_line\n"
            "second:\n  message: |\n    # beta_payload\n    body_line\n"
        )
        assert self._sweep(
            tmp_path, ["messages.yaml:2-4"], ["messages.yaml"]
        ).candidates == []

    def test_yaml_plain_scalar_spacing_is_visible(self, tmp_path):
        (tmp_path / "values.yaml").write_text(
            "first:\n  message: hello  world\n  level: verbose_mode\n"
            "second:\n  message: hello world\n  level: verbose_mode\n"
        )
        assert self._sweep(
            tmp_path, ["values.yaml:2-3"], ["values.yaml"]
        ).candidates == []

    def test_makefile_recipe_tabs_are_visible(self, tmp_path):
        (tmp_path / "Makefile").write_text(
            "target_one:\n\texport build_mode=enabled\n\trun_step_two\n"
            "export build_mode=enabled\nrun_step_two\n"
        )
        assert self._sweep(tmp_path, ["Makefile:2-3"], ["Makefile"]).candidates == []

    def test_sql_dollar_quoted_payloads_are_visible(self, tmp_path):
        block = "SELECT $body$\n-- PAYLOAD\nshared_statement\n$body$ FROM records\n"
        (tmp_path / "fn.sql").write_text(
            block.replace("PAYLOAD", "alpha_payload")
            + block.replace("PAYLOAD", "beta_payload")
        )
        assert self._sweep(tmp_path, ["fn.sql:1-4"], ["fn.sql"]).candidates == []

    def test_block_comments_do_not_weld_tokens(self, tmp_path):
        (tmp_path / "report.sql").write_text(
            "SELECT account/**/display_name FROM records\nWHERE archived_at IS NULL\n"
            "SELECT accountdisplay_name FROM records\nWHERE archived_at IS NULL\n"
        )
        assert self._sweep(tmp_path, ["report.sql:1-2"], ["report.sql"]).candidates == []

    def test_markdown_urls_survive_intact(self, tmp_path):
        (tmp_path / "docs.md").write_text(
            "Docs at https://alpha.example/path\nShared trailing sentence here\n"
            "Docs at https://beta.example/path\nShared trailing sentence here\n"
        )
        assert self._sweep(tmp_path, ["docs.md:1-2"], ["docs.md"]).candidates == []

    def test_regex_literal_slashes_survive_intact(self, tmp_path):
        (tmp_path / "routes.js").write_text(
            "const routePattern = /[//]alpha/;\nregisterRoute(routePattern);\n"
            "const routePattern = /[//]beta/;\nregisterRoute(routePattern);\n"
        )
        assert self._sweep(tmp_path, ["routes.js:1-2"], ["routes.js"]).candidates == []

    def test_tool_directives_survive_intact(self, tmp_path):
        (tmp_path / "imports.js").write_text(
            'const mod = import(/* webpackMode: "eager" */ "./alpha");\n'
            "registerModule(mod);\n"
            'const mod = import(/* webpackMode: "lazy" */ "./alpha");\n'
            "registerModule(mod);\n"
        )
        assert self._sweep(tmp_path, ["imports.js:1-2"], ["imports.js"]).candidates == []

    # --- what raw matching still reports ------------------------------------

    def test_identical_blocks_still_mirror(self, tmp_path):
        (tmp_path / "same.sh").write_text(self._heredoc("alpha") * 2)
        result = self._sweep(tmp_path, ["same.sh:2-5"], ["same.sh"])
        assert [c["site"] for c in result.candidates] == ["same.sh:8-11"]

    def test_identical_makefile_blocks_still_mirror(self, tmp_path):
        (tmp_path / "Makefile").write_text(
            "one:\n\texport build_mode=enabled\n\trun_step_two\n"
            "two:\n\texport build_mode=enabled\n\trun_step_two\n"
        )
        result = self._sweep(tmp_path, ["Makefile:2-3"], ["Makefile"])
        assert [c["site"] for c in result.candidates] == ["Makefile:5-6"]

    def test_a_comment_difference_costs_the_mirror_outside_python(self, tmp_path):
        """The accepted recall cost: unsupported languages get exact matching
        only, so a duplicate carrying a different comment is not reported."""
        (tmp_path / "notes.js").write_text(
            "const mod = loadModule(); /* alpha note */\nregisterModule(mod);\n"
            "const mod = loadModule(); /* beta note */\nregisterModule(mod);\n"
        )
        assert self._sweep(tmp_path, ["notes.js:1-2"], ["notes.js"]).candidates == []

    # --- language scoping ---------------------------------------------------

    def test_a_seed_never_matches_another_language(self, tmp_path):
        body = "export build_mode=enabled\ninclude common.mk\n"
        (tmp_path / "build.sh").write_text(body)
        (tmp_path / "Makefile").write_text(body)
        assert self._sweep(
            tmp_path, ["build.sh:1-2"], ["build.sh", "Makefile"]
        ).candidates == []

    @pytest.mark.parametrize("other,expected", [
        ("b.ts", "b.ts:1-2"),      # same ecmascript family
        ("b.mjs", "b.mjs:1-2"),    # same ecmascript family
    ])
    def test_a_seed_matches_its_own_language_family(self, tmp_path, other, expected):
        body = "const parsed = parsePayload(raw);\nemitResult(parsed);\n"
        (tmp_path / "a.js").write_text(body)
        (tmp_path / other).write_text(body)
        result = self._sweep(tmp_path, ["a.js:1-2"], ["a.js", other])
        assert [c["site"] for c in result.candidates] == [expected]

    def test_yaml_suffix_aliases_are_one_family(self, tmp_path):
        body = "image: alpine_base\ncommand: serve_http\n"
        (tmp_path / "a.yaml").write_text(body)
        (tmp_path / "b.yml").write_text(body)
        result = self._sweep(tmp_path, ["a.yaml:1-2"], ["a.yaml", "b.yml"])
        assert [c["site"] for c in result.candidates] == ["b.yml:1-2"]

    # --- documented limitation ---------------------------------------------

    def test_identical_text_in_a_js_literal_is_a_known_false_positive(self, tmp_path):
        """Accepted limitation, not a bug to fix here.

        Raw matching has no lexical context, so lines inside a template literal
        that spell out the same calls made elsewhere still match. Detecting this
        needs a real JS parser -- the complexity boundary this module stays
        behind. Python has no such gap (see TestPythonNormalizedMatching).
        """
        (tmp_path / "runner.js").write_text(
            "const template = `\nalpha_task()\nbeta_task()\n`;\n"
            "alpha_task()\nbeta_task()\n"
        )
        result = self._sweep(tmp_path, ["runner.js:2-3"], ["runner.js"])
        assert [c["site"] for c in result.candidates] == ["runner.js:5-6"]

    # --- language-agnostic guards, unchanged by the mode split --------------

    def test_an_overlong_seed_is_refused_rather_than_scanned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sweeper, "MAX_MIRROR_BLOCK_LINES", 3)
        block = "alpha_one();\nbeta_two();\ngamma_three();\ndelta_four();\n"
        (tmp_path / "long.js").write_text(block * 2)
        assert self._sweep(tmp_path, ["long.js:1-4"], ["long.js"]).candidates == []

    def test_a_seed_within_the_cap_still_mirrors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sweeper, "MAX_MIRROR_BLOCK_LINES", 4)
        block = "alpha_one();\nbeta_two();\ngamma_three();\ndelta_four();\n"
        (tmp_path / "long.js").write_text(block * 2)
        result = self._sweep(tmp_path, ["long.js:1-4"], ["long.js"])
        assert [c["site"] for c in result.candidates] == ["long.js:5-8"]

    def test_differing_non_utf8_bytes_are_not_reported_as_a_mirror(self, tmp_path):
        (tmp_path / "legacy.js").write_bytes(
            b'const label = "caf\xe9";\nconst other = compute_value();\n'
            b'const label = "caf\xe8";\nconst other = compute_value();\n'
        )
        assert self._sweep(tmp_path, ["legacy.js:1-2"], ["legacy.js"]).candidates == []

    def test_the_same_block_in_valid_utf8_does_mirror(self, tmp_path):
        (tmp_path / "clean.js").write_bytes(
            'const label = "café";\nconst other = compute_value();\n'.encode() * 2
        )
        result = self._sweep(tmp_path, ["clean.js:1-2"], ["clean.js"])
        assert [c["site"] for c in result.candidates] == ["clean.js:3-4"]

    def test_lossy_files_are_still_readable_for_token_sweeps(self, tmp_path):
        path = tmp_path / "legacy.py"
        path.write_bytes(b'value = "caf\xe9".read_text().encode()\n')
        assert sweeper._is_lossy_text(path) is True
        assert sweeper._read_lines(path) == ['value = "caf�".read_text().encode()']

    def test_one_copy_is_reported_once_across_seeds_of_different_lengths(
        self, tmp_path
    ):
        (tmp_path / "copy.js").write_text(
            "alpha_step();\nbeta_step();\ngamma_step();\nunrelated_call();\n"
            "alpha_step();\nbeta_step();\ngamma_step();\n"
        )
        result = sweeper.sweep(
            signature="prefix", label="shared prefix",
            sites=["copy.js:1-2", "copy.js:1-3"],
            changed_files=["copy.js"], root=tmp_path,
        )
        mirrors = [c for c in result.candidates if c["candidateClass"] == "mirror"]
        assert [c["site"] for c in mirrors] == ["copy.js:5-7"]

    def test_partially_overlapping_copies_are_unioned_not_dropped(self, tmp_path):
        """Seeds 1-3 and 2-4 match at 6-8 and 7-9. The duplicated region is
        6-9; keeping only the first would under-report it by a line."""
        (tmp_path / "copy.js").write_text(
            "alpha_step();\nbeta_step();\ngamma_step();\ndelta_step();\n"
            "unrelated_call();\n"
            "alpha_step();\nbeta_step();\ngamma_step();\ndelta_step();\n"
        )

        result = sweeper.sweep(
            signature="overlap", label="overlap",
            sites=["copy.js:1-3", "copy.js:2-4"],
            changed_files=["copy.js"], root=tmp_path,
        )

        mirrors = [c for c in result.candidates if c["candidateClass"] == "mirror"]
        assert [c["site"] for c in mirrors] == ["copy.js:6-9"]

    def test_line_endings_are_a_documented_blind_spot(self, tmp_path):
        """Accepted limitation: splitlines() drops the terminator, so an LF and
        a CRLF block with the same content still match in raw mode."""
        (tmp_path / "a.sh").write_bytes(b"export build_mode=enabled\nrun_step_two\n")
        (tmp_path / "b.sh").write_bytes(b"export build_mode=enabled\r\nrun_step_two\r\n")

        result = self._sweep(tmp_path, ["a.sh:1-2"], ["a.sh", "b.sh"])

        assert [c["site"] for c in result.candidates] == ["b.sh:1-2"]


class TestPythonNormalizedMatching:
    """The one explicitly supported language. Comments come off via Python's
    own tokenizer; whitespace does not, because indentation is semantic."""

    @staticmethod
    def _sweep(tmp_path, sites, changed_files):
        return sweeper.sweep(
            signature="py", label="py",
            sites=sites, changed_files=changed_files, root=tmp_path,
        )

    def test_comments_do_not_break_a_python_mirror(self, tmp_path):
        (tmp_path / "loader.py").write_text(
            "def load_alpha(path):  # cached\n"
            "    payload = path.read_text()\n"
            "    return payload.encode()\n"
            "def load_beta(path):  # uncached\n"
            "    payload = path.read_text()\n"
            "    return payload.encode()\n"
        )
        result = self._sweep(tmp_path, ["loader.py:2-3"], ["loader.py"])
        assert [c["site"] for c in result.candidates] == ["loader.py:5-6"]

    def test_comment_only_and_blank_lines_do_not_break_a_python_mirror(self, tmp_path):
        (tmp_path / "gapped.py").write_text(
            "payload = path.read_text()\n"
            "# an explanatory note\n"
            "\n"
            "result = payload.encode()\n"
            "payload = path.read_text()\n"
            "result = payload.encode()\n"
        )
        result = self._sweep(tmp_path, ["gapped.py:1-4"], ["gapped.py"])
        assert [c["site"] for c in result.candidates] == ["gapped.py:5-6"]

    def test_indentation_still_distinguishes_python_blocks(self, tmp_path):
        (tmp_path / "levels.py").write_text(
            "if outer_ready:\n"
            "    payload = path.read_text()\n"
            "    result = payload.encode()\n"
            "if other_ready:\n"
            "        payload = path.read_text()\n"
            "        result = payload.encode()\n"
        )
        assert self._sweep(tmp_path, ["levels.py:2-3"], ["levels.py"]).candidates == []

    def test_internal_spacing_still_distinguishes_python_blocks(self, tmp_path):
        (tmp_path / "spacing.py").write_text(
            'label = "hello  world"\n'
            "result = label.encode()\n"
            'label = "hello world"\n'
            "result = label.encode()\n"
        )
        assert self._sweep(tmp_path, ["spacing.py:1-2"], ["spacing.py"]).candidates == []

    def test_a_docstring_body_does_not_mirror_the_code_it_quotes(self, tmp_path):
        """tokenize tells us which lines are string bodies, so Python does not
        have the residual false positive that raw-mode languages do."""
        (tmp_path / "quoted.py").write_text(
            "USAGE = '''\n"
            "alpha_task()\n"
            "beta_task()\n"
            "'''\n"
            "alpha_task()\n"
            "beta_task()\n"
        )
        assert self._sweep(tmp_path, ["quoted.py:2-3"], ["quoted.py"]).candidates == []

    def test_python_never_matches_a_non_python_file(self, tmp_path):
        body = "payload = path.read_text()\nresult = payload.encode()\n"
        (tmp_path / "a.py").write_text(body)
        (tmp_path / "b.txt").write_text(body)
        assert self._sweep(tmp_path, ["a.py:1-2"], ["a.py", "b.txt"]).candidates == []

    def test_unparseable_python_falls_back_to_raw_matching(self, tmp_path):
        """A file mid-edit still gets exact matching rather than nothing."""
        broken = "def broken(:\n    payload = path.read_text()\n"
        (tmp_path / "broken.py").write_text(broken * 2)

        assert sweeper._python_block_lines(broken.split("\n")) is None
        result = self._sweep(tmp_path, ["broken.py:1-2"], ["broken.py"])
        assert [c["site"] for c in result.candidates] == ["broken.py:3-4"]

    def test_trailing_spaces_inside_a_string_are_part_of_the_value(self, tmp_path):
        """tokenize tags these lines as string data, so normalization must not
        touch them -- two fixtures differing only in trailing spaces generate
        different text and are not duplicates."""
        (tmp_path / "fixtures.py").write_text(
            "ALPHA = '''\nfirst_column  second_column   \nthird_column\n'''\n"
            "BETA = '''\nfirst_column  second_column\nthird_column\n'''\n"
        )

        result = self._sweep(tmp_path, ["fixtures.py:2-3"], ["fixtures.py"])

        assert result.candidates == []

    def test_blank_lines_inside_a_string_are_part_of_the_value(self, tmp_path):
        (tmp_path / "blanks.py").write_text(
            "ALPHA = '''\nfirst_column\n\nsecond_column\n'''\n"
            "BETA = '''\nfirst_column\nsecond_column\n'''\n"
        )

        result = self._sweep(tmp_path, ["blanks.py:2-4"], ["blanks.py"])

        assert result.candidates == []

    def test_identical_string_bodies_still_mirror(self, tmp_path):
        """Preserving string bytes must not stop real duplicates matching."""
        body = "ALPHA = '''\nfirst_column  second_column   \nthird_column\n'''\n"
        (tmp_path / "same.py").write_text(body + body.replace("ALPHA", "BETA"))

        result = self._sweep(tmp_path, ["same.py:2-3"], ["same.py"])

        assert [c["site"] for c in result.candidates] == ["same.py:6-7"]

    @pytest.mark.parametrize("first,second", [
        ("# type: ignore[arg-type]", "# type: ignore[return-value]"),
        ("# noqa: F821", "# nosec"),
        ("# pragma: no cover", "# pragma: no branch"),
        ("# pylint: disable=no-member", "# pylint: disable=protected-access"),
        # Families no vendor list ever named -- matched by shape, not by name.
        ("# cython: boundscheck=False", "# cython: boundscheck=True"),
        ("# distutils: language=c++", "# distutils: language=c"),
        ("# doctest: +SKIP", "# doctest: +ELLIPSIS"),
        ("# numba: nopython=True", "# numba: nopython=False"),
    ])
    def test_differing_tool_directives_are_not_duplicates(
        self, tmp_path, first, second
    ):
        """A directive is read by mypy/flake8/bandit/coverage, so removing it
        changes behavior. tokenize cannot tell us that -- Python has no notion
        of a directive -- hence the explicit prefix list."""
        (tmp_path / "d.py").write_text(
            f"result = compute(payload)  {first}\n"
            "emit_result(result)\n"
            f"result = compute(payload)  {second}\n"
            "emit_result(result)\n"
        )

        assert self._sweep(tmp_path, ["d.py:1-2"], ["d.py"]).candidates == []

    def test_a_python_block_never_mirrors_into_another_file(self, tmp_path):
        """The boundary. Normalized matching drops comments, and once they are
        gone identical text can still mean different things in two files, for
        reasons with no end. Within one file they cannot differ at all."""
        body = "payload = read_source()  # cached\nemit_payload(payload)\n"
        (tmp_path / "a.py").write_text(body)
        (tmp_path / "b.py").write_text(body.replace("# cached", "# uncached"))

        result = self._sweep(tmp_path, ["a.py:1-2"], ["a.py", "b.py"])

        assert result.candidates == []

    def test_byte_identical_python_files_still_mirror_as_exact(self, tmp_path):
        """Scoping *normalized* matching to one file must not cost raw matching
        its reach. A Python file carries both indexes, so a byte-identical copy
        in another file is still reported -- as `exact`, which claims only that
        the bytes repeat and so is safe across files."""
        body = "payload = read_source()\nemit_payload(payload)\n"
        (tmp_path / "a.py").write_text(body)
        (tmp_path / "b.py").write_text(body)

        result = self._sweep(tmp_path, ["a.py:1-2"], ["a.py", "b.py"])

        assert [c["site"] for c in result.candidates] == ["b.py:1-2"]
        assert result.candidates[0]["matchMode"] == "exact"

    def test_a_same_file_byte_identical_block_is_reported_as_exact(self, tmp_path):
        """Both indexes find it. `exact` is the stronger claim, so it wins --
        otherwise the weaker label would depend on seed ordering."""
        body = "payload = read_source()\nemit_payload(payload)\n"
        (tmp_path / "a.py").write_text(body + "spacer()\n" + body)

        result = self._sweep(tmp_path, ["a.py:1-2"], ["a.py"])

        assert [c["site"] for c in result.candidates] == ["a.py:4-5"]
        assert result.candidates[0]["matchMode"] == "exact"

    @pytest.mark.parametrize("name,a_text,b_text", [
        (
            "differing shebangs",
            "#!/usr/bin/python3\n",
            "#!/usr/bin/python2\n",
        ),
        (
            "differing encodings",
            "# coding: utf-8\n",
            "# coding: latin-1\n",
        ),
        (
            "differing future flags",
            "from __future__ import annotations\n",
            "import os\n",
        ),
    ])
    def test_file_level_semantics_never_produce_a_normalized_cross_file_mirror(
        self, tmp_path, name, a_text, b_text
    ):
        """Each of these was once its own patch to the match key. Scoping the
        normalized index to one file retires the whole class, including the
        members nobody has named yet.

        The bodies here are byte-identical, so raw matching still reports them
        -- and that is the intended split. `exact` claims only that the bytes
        repeat, which stays true whatever the shebang or encoding says; it is
        `normalized`, the claim that survives an edit to the comments, that
        must never cross a file boundary."""
        body = "payload = read_source()\nemit_payload(payload)\n"
        (tmp_path / "a.py").write_text(a_text + body)
        (tmp_path / "b.py").write_text(b_text + body)

        result = self._sweep(tmp_path, ["a.py:2-3"], ["a.py", "b.py"])

        assert [c["matchMode"] for c in result.candidates] == ["exact"], name

    @pytest.mark.parametrize("a_comment,b_comment", [
        ("  # cached", "  # uncached"),
        ("  # see ticket 41", "  # see ticket 92"),
    ])
    def test_comment_differences_never_cross_a_file_boundary(
        self, tmp_path, a_comment, b_comment
    ):
        """The other half of the split: once the bytes differ, only the
        normalized index could match these, and it does not leave the file."""
        body = "payload = read_source(){}\nemit_payload(payload)\n"
        (tmp_path / "a.py").write_text(body.format(a_comment))
        (tmp_path / "b.py").write_text(body.format(b_comment))

        result = self._sweep(tmp_path, ["a.py:1-2"], ["a.py", "b.py"])

        assert result.candidates == []

    def test_a_stub_is_not_a_mirror_of_a_runtime_module(self, tmp_path):
        """`def parse(...) -> str: ...` declares in a `.pyi` and returns
        Ellipsis in a `.py`. Same text, different meaning, different files."""
        body = "def parse(raw_value):\n    return transform_value(raw_value)\n"
        (tmp_path / "a.py").write_text(body)
        (tmp_path / "b.pyi").write_text(body)

        result = self._sweep(tmp_path, ["a.py:1-2"], ["a.py", "b.pyi"])

        assert result.candidates == []

    def test_raw_matching_still_crosses_files(self, tmp_path):
        """The recall that pays for the rule above: byte-identical blocks in
        two files are still reported, because raw mode claims only that the
        bytes repeat."""
        body = "const total = accumulate(entries);\nreturn formatTotal(total);\n"
        (tmp_path / "a.js").write_text(body)
        (tmp_path / "b.js").write_text(body)

        result = self._sweep(tmp_path, ["a.js:1-2"], ["a.js", "b.js"])

        assert [c["site"] for c in result.candidates] == ["b.js:1-2"]
        assert result.candidates[0]["matchMode"] == "exact"

    def test_identical_directives_still_mirror(self, tmp_path):
        (tmp_path / "same.py").write_text(
            "result = compute(payload)  # type: ignore[arg-type]\n"
            "emit_result(result)\n"
            "result = compute(payload)  # type: ignore[arg-type]\n"
            "emit_result(result)\n"
        )

        result = self._sweep(tmp_path, ["same.py:1-2"], ["same.py"])

        assert [c["site"] for c in result.candidates] == ["same.py:3-4"]

    def test_a_directive_only_line_is_content(self, tmp_path):
        """Ordinary comment-only lines drop out; a directive-only line does
        not, because its presence changes what the tools do."""
        (tmp_path / "only.py").write_text(
            "# pragma: no cover\npayload = read_source()\nemit(payload)\n"
            "payload = read_source()\nemit(payload)\n"
        )

        assert self._sweep(tmp_path, ["only.py:1-3"], ["only.py"]).candidates == []

    def test_an_ordinary_comment_is_not_mistaken_for_a_directive(self, tmp_path):
        (tmp_path / "prose.py").write_text(
            "payload = read_source()  # typically cached\n"
            "emit(payload)\n"
            "payload = read_source()  # typically not\n"
            "emit(payload)\n"
        )

        result = self._sweep(tmp_path, ["prose.py:1-2"], ["prose.py"])

        assert [c["site"] for c in result.candidates] == ["prose.py:3-4"]

    @pytest.mark.parametrize("first,second", [
        ("# Defensive: guards a future plumbing path", "# Defensive: guards bad input"),
        ("# Default: every reader returns None", "# Default: readers may raise"),
        ("# TODO: revisit once cached", "# TODO: revisit after the refactor"),
        ("# Note: this path is hot", "# Note: this path is cold"),
    ])
    def test_capitalized_prose_is_still_an_ordinary_comment(
        self, tmp_path, first, second
    ):
        """Case is what separates a directive from prose: tools write
        `# cython:` lowercase, English capitalizes. Without that, every
        `# Note: ...` would be treated as meaningful."""
        (tmp_path / "prose.py").write_text(
            f"payload = read_source()  {first}\n"
            "emit(payload)\n"
            f"payload = read_source()  {second}\n"
            "emit(payload)\n"
        )

        result = self._sweep(tmp_path, ["prose.py:1-2"], ["prose.py"])

        assert [c["site"] for c in result.candidates] == ["prose.py:3-4"]

    def test_lowercase_prose_with_a_colon_costs_a_mirror(self, tmp_path):
        """The accepted cost of matching by shape. `# invariant: ...` is prose,
        but it is shaped exactly like a directive, so it is kept and the two
        blocks no longer match. Erring this way loses a duplicate rather than
        inventing one."""
        (tmp_path / "shape.py").write_text(
            "payload = read_source()  # invariant: count == len(sites)\n"
            "emit(payload)\n"
            "payload = read_source()  # invariant: count == len(rows)\n"
            "emit(payload)\n"
        )

        assert self._sweep(tmp_path, ["shape.py:1-2"], ["shape.py"]).candidates == []

    def test_a_parseable_file_is_never_compared_against_an_unparseable_one(
        self, tmp_path
    ):
        """Different keys: one indexes as python, the other as raw .py text."""
        body = "payload = path.read_text()\nresult = payload.encode()\n"
        (tmp_path / "good.py").write_text(body)
        (tmp_path / "bad.py").write_text("def broken(:\n" + body)

        result = self._sweep(tmp_path, ["good.py:1-2"], ["good.py", "bad.py"])
        assert result.candidates == []


class TestMirrorTokenInteraction:
    """The token phase skips lines already covered by a mirror. It walks the
    mirror ranges with a moving pointer, so the pointer must stay in step with
    the line number across several disjoint ranges."""

    @staticmethod
    def _repo(tmp_path):
        (tmp_path / "f.py").write_text(
            "value = path.read_text().encode()\nemit(value)\nspacer_one()\n"     # 1-3
            "value = path.read_text().encode()\nemit(value)\nspacer_two()\n"     # 4-6
            "value = path.read_text().encode()\nemit(value)\nspacer_three()\n"   # 7-9
            "value = path.read_text().encode()\nemit(value)\nspacer_four()\n"    # 10-12
            "emit(value)\n"                                                      # 13
        )
        return sweeper.sweep(
            signature="x", label="x",
            sites=["f.py:1-2", "f.py:4-5"],
            changed_files=["f.py"], root=tmp_path,
        )

    def test_token_hits_inside_any_mirror_range_are_suppressed(self, tmp_path):
        result = self._repo(tmp_path)
        mirrors = [
            (c["line"], c["endLine"])
            for c in result.candidates if c["candidateClass"] == "mirror"
        ]
        tokens = [c["line"] for c in result.candidates if c["candidateClass"] == "token"]

        assert len(mirrors) > 1, "need several ranges to exercise the pointer"
        assert not [
            line for line in tokens
            if any(start <= line <= end for start, end in mirrors)
        ]

    def test_token_hits_outside_every_mirror_range_survive(self, tmp_path):
        """The pointer must not over-suppress: line 13 follows the last mirror
        and is a genuine token sibling."""
        result = self._repo(tmp_path)

        assert [c["site"] for c in result.candidates] == [
            "f.py:7-8", "f.py:10-11", "f.py:13",
        ]

    def test_suppression_stays_linear_in_file_length(self, tmp_path):
        """Scanning every mirror range per line was quadratic: a file of
        repeated blocks produces a mirror hit per block. The bound is loose --
        it is here to catch a return to quadratic, not to measure speed."""
        import time

        blocks = 25_000
        (tmp_path / "rep.py").write_text(
            "value = path.read_text().encode()\nemit(value)\n" * blocks
        )

        started = time.perf_counter()
        result = sweeper.sweep(
            signature="x", label="x",
            sites=["rep.py:1-2", "rep.py:3-4"],
            changed_files=["rep.py"], root=tmp_path,
        )
        elapsed = time.perf_counter() - started

        assert result.status == "ok"
        assert elapsed < 10, f"{blocks} blocks took {elapsed:.1f}s — quadratic again?"


class TestReport:
    def test_report_names_the_sites_and_the_shared_shape(self, repo):
        result = sweeper.sweep(
            signature="utf8", label="missing decode guard",
            sites=["app/config.py:2", "app/config.py:6"],
            changed_files=["app/config.py"], root=repo,
        )
        report = sweeper.render_report(result)
        assert "app/config.py:10" in report
        assert "encode" in report
        assert "flagged:" in report

    def test_report_says_so_when_there_is_nothing_to_sweep(self, repo):
        result = sweeper.sweep(
            signature="salt", label="salt encode",
            sites=["app/auth.py:2", "app/auth.py:2"],
            changed_files=["app/auth.py"], root=repo,
        )
        report = sweeper.render_report(result)
        assert "skipped:" in report or "none" in report

    def test_skipped_report_explains_why(self, repo):
        result = sweeper.sweep(
            signature="utf8", label="one site",
            sites=["app/config.py:2"], changed_files=["app/config.py"], root=repo,
        )
        assert "skipped:" in sweeper.render_report(result)

    def test_report_labels_a_mirror_candidate(self, mirror_repo):
        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["lib/options.ts:5-8"],
            changed_files=[fixture["path"] for fixture in MIRROR_CORPUS],
            root=mirror_repo,
        )

        # TS/JS are one family but not a supported normalized language, so this
        # match is text-identical and the report says so.
        assert "[mirror exact]" in sweeper.render_report(result)

    def test_report_labels_a_normalized_mirror_as_such(self, tmp_path):
        (tmp_path / "loader.py").write_text(
            "payload = path.read_text()  # cached\n"
            "result = payload.encode()\n"
            "payload = path.read_text()\n"
            "result = payload.encode()\n"
        )
        result = sweeper.sweep(
            signature="loader", label="loader",
            sites=["loader.py:1-2"], changed_files=["loader.py"], root=tmp_path,
        )

        assert "[mirror normalized]" in sweeper.render_report(result)


class TestCli:
    def test_json_stdout_is_json_only(self, repo, capsys):
        rc = sweeper.main([
            "--signature", "utf8", "--label", "missing decode guard",
            "--site", "app/config.py:2", "--site", "app/config.py:6",
            "--changed-file", "app/config.py",
            "--root", str(repo), "--json",
        ])
        out = capsys.readouterr()
        payload = json.loads(out.out)
        assert rc == 0
        assert out.err == ""
        assert payload["status"] == "ok"
        assert payload["candidates"][0]["site"] == "app/config.py:10"

    def test_human_output_is_the_report(self, repo, capsys):
        rc = sweeper.main([
            "--signature", "utf8",
            "--site", "app/config.py:2", "--site", "app/config.py:6",
            "--changed-file", "app/config.py", "--root", str(repo),
        ])
        assert rc == 0
        assert "[sweep]" in capsys.readouterr().out

    def test_at_least_two_sites_are_required_end_to_end(self, repo, capsys):
        rc = sweeper.main([
            "--signature", "utf8", "--site", "app/config.py:2",
            "--changed-file", "app/config.py", "--root", str(repo), "--json",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["status"] == "too_few_sites"
        assert payload["candidates"] == []


class TestCommentLines:
    """A reviewer finding anchors to code, so a comment is never a sibling.

    Prose shares ordinary words with code, which is the one false-positive
    class a token intersection cannot rule out on its own. Found by fuzzing
    random line pairs: a docstring mentioning `state.json` matched a
    `json.dumps` call and proposed comment lines as siblings.
    """

    @pytest.mark.parametrize("line,is_code", [
        ("    data = json.loads(x)", True),
        ("# state.json must be a JSON object", False),
        ("    # trailing thought", False),
        ("// javascript comment", False),
        (" * javadoc continuation", False),
        ("-- sql comment", False),
        ("; lisp comment", False),
        ("", False),
        ("    ", False),
        ("value = 1  # inline comments do not disqualify the line", True),
    ])
    def test_classifies_code_versus_comment(self, line, is_code):
        assert sweeper.is_code_line(line) is is_code

    def test_a_comment_is_never_reported_as_a_sibling(self, tmp_path):
        (tmp_path / "m.py").write_text(
            "data = json.loads(path.read_text())\n"
            "more = json.loads(path.read_text())\n"
            "# json.loads(path.read_text()) is what this used to do\n"
            "other = json.loads(path.read_text())\n"
        )
        result = sweeper.sweep(
            signature="s", label="json.loads", sites=["m.py:1", "m.py:2"],
            changed_files=["m.py"], root=tmp_path,
        )
        assert [c["line"] for c in result.candidates] == [4]

    def test_flagged_sites_pointing_at_comments_do_not_sweep(self, tmp_path):
        (tmp_path / "m.py").write_text(
            "# the config is loaded from state.json here\n"
            "# and state.json is written back on exit\n"
            "data = json.loads(path.read_text())\n"
        )
        result = sweeper.sweep(
            signature="s", label="prose", sites=["m.py:1", "m.py:2"],
            changed_files=["m.py"], root=tmp_path,
        )
        assert result.status == "no_source"
        assert result.candidates == []


class TestSpansByPath:
    """Flagged and mirrored ranges are held as intervals, not as a set of every
    line they cover: an anchor may span an entire file, and the moving-pointer
    membership test the sweep uses requires the intervals to be disjoint."""

    def test_overlapping_ranges_merge(self):
        spans = sweeper._spans_by_path([("a.py", 1, 10), ("a.py", 5, 20)])
        assert spans == {"a.py": [(1, 20)]}

    def test_adjacent_ranges_merge(self):
        spans = sweeper._spans_by_path([("a.py", 1, 4), ("a.py", 5, 9)])
        assert spans == {"a.py": [(1, 9)]}

    def test_disjoint_ranges_stay_separate_and_sorted(self):
        spans = sweeper._spans_by_path([("a.py", 40, 45), ("a.py", 1, 3)])
        assert spans == {"a.py": [(1, 3), (40, 45)]}

    def test_paths_are_kept_apart(self):
        spans = sweeper._spans_by_path([("a.py", 1, 3), ("b.py", 1, 3)])
        assert spans == {"a.py": [(1, 3)], "b.py": [(1, 3)]}

    def test_a_whole_file_range_costs_one_interval(self):
        """The property that makes this cheap: cost tracks the number of
        findings, not the number of lines they cover."""
        spans = sweeper._spans_by_path(
            [("big.py", 1, 300_000), ("big.py", 2, 300_000)]
        )
        assert spans == {"big.py": [(1, 300_000)]}

    def test_membership_matches_the_set_it_replaces(self):
        ranges = [("a.py", 3, 7), ("a.py", 6, 9), ("a.py", 20, 21)]
        spans = sweeper._spans_by_path(ranges)["a.py"]
        expanded = {n for _p, lo, hi in ranges for n in range(lo, hi + 1)}
        for line in range(1, 30):
            assert (any(lo <= line <= hi for lo, hi in spans)) == (line in expanded)

    def test_overlapping_flagged_ranges_are_all_suppressed(self, tmp_path):
        """Two anchors overlapping in one file must not leave a hole: the
        moving pointer would skip lines if the intervals were not merged."""
        (tmp_path / "f.py").write_text(
            "alpha = compute(seed)\n"
            "beta = compute(seed)\n"
            "gamma = compute(seed)\n"
            "delta = compute(seed)\n"
        )
        result = sweeper.sweep(
            signature="s", label="overlap", sites=["f.py:1-2", "f.py:2-3"],
            changed_files=["f.py"], root=tmp_path,
        )
        assert [c["site"] for c in result.candidates] == ["f.py:4"]


class TestPathContainment:
    """The changed-file list comes from a pull request, so it is untrusted.
    A path in it must not be able to reach a file the diff never contained --
    the sweep quotes what it reads into a report."""

    SECRET = "API_TOKEN = load_credential()\nsubmit_credential(API_TOKEN)\n"

    @pytest.fixture
    def repo_and_outside(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text(self.SECRET)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "real.py").write_text(self.SECRET)
        (repo / "inside.py").write_text(self.SECRET)
        return repo, outside

    @staticmethod
    def _sweep(repo, changed):
        return sweeper.sweep(
            signature="s", label="containment", sites=["real.py:1-2"],
            changed_files=changed, root=repo,
        )

    def test_a_symlink_out_of_the_repository_is_not_read(self, repo_and_outside, symlinks_available):
        """Git records the link in the diff, mode 120000 -- not the bytes it
        points at. Following it scans a file outside the PR entirely."""
        repo, outside = repo_and_outside
        (repo / "escape.py").symlink_to(outside / "secret.py")

        assert self._sweep(repo, ["real.py", "escape.py"]).candidates == []

    def test_a_symlink_inside_the_repository_is_also_refused(self, repo_and_outside, symlinks_available):
        """Still nothing reviewable in it: the diff carries the link, so the
        target's text belongs to whichever real path already holds it."""
        repo, _outside = repo_and_outside
        (repo / "internal.py").symlink_to(repo / "inside.py")

        assert self._sweep(repo, ["real.py", "internal.py"]).candidates == []

    def test_a_parent_traversal_path_is_refused(self, repo_and_outside):
        """No symlink needed -- the path itself climbs out."""
        repo, outside = repo_and_outside

        result = self._sweep(repo, ["real.py", f"../{outside.name}/secret.py"])

        assert result.candidates == []

    def test_an_absolute_path_is_refused(self, repo_and_outside):
        """`Path("/repo") / "/etc/passwd"` is `/etc/passwd`; the root is
        silently discarded, so containment has to be checked after joining."""
        repo, outside = repo_and_outside

        result = self._sweep(repo, ["real.py", str(outside / "secret.py")])

        assert result.candidates == []

    def test_a_symlinked_intermediate_directory_is_refused(self, repo_and_outside, symlinks_available):
        """The final component is a real file; it is a parent that escapes."""
        repo, outside = repo_and_outside
        (repo / "sub").mkdir()
        (repo / "sub" / "outdir").symlink_to(outside, target_is_directory=True)

        result = self._sweep(repo, ["real.py", "sub/outdir/secret.py"])

        assert result.candidates == []

    def test_an_ordinary_file_is_still_read(self, repo_and_outside):
        """The check must not cost the sweep its actual job."""
        repo, _outside = repo_and_outside

        result = self._sweep(repo, ["real.py", "inside.py"])

        assert [c["site"] for c in result.candidates] == ["inside.py:1-2"]

    def test_containment_helper_accepts_a_nested_real_path(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")

        assert sweeper._contained_path(tmp_path, "pkg/mod.py") is not None

    @pytest.mark.parametrize("rel", ["../elsewhere.py", "/etc/passwd"])
    def test_containment_helper_rejects_escapes(self, tmp_path, rel):
        assert sweeper._contained_path(tmp_path, rel) is None


class TestMergedEvidence:
    """A merged range describes all the lines it now covers, so its claim has
    to hold across all of them."""

    @staticmethod
    def _hit(mode, line, end):
        return {"candidateClass": "mirror", "matchMode": mode, "path": "f.py",
                "line": line, "endLine": end, "site": f"f.py:{line}-{end}",
                "text": "t", "fingerprint": "x"}

    @pytest.mark.parametrize("first,second,expected", [
        ("exact", "normalized", "normalized"),
        ("normalized", "exact", "normalized"),
        ("exact", "exact", "exact"),
        ("normalized", "normalized", "normalized"),
    ])
    def test_a_union_is_only_as_strong_as_its_weakest_part(
        self, first, second, expected
    ):
        """`exact` over the union would claim the bytes repeat across lines
        that only matched once comments were removed."""
        merged = sweeper._merge_overlapping(
            [self._hit(first, 10, 11), self._hit(second, 11, 13)]
        )

        assert [c["site"] for c in merged] == ["f.py:10-13"]
        assert merged[0]["matchMode"] == expected


class TestCandidateCap:
    """The cap decides how much is reported. It must not decide it by
    filename, or the strongest finding can vanish while `truncated` implies
    nothing of consequence was lost."""

    @staticmethod
    def _token(index):
        return {"candidateClass": "token", "path": "a.py", "line": index,
                "site": f"a.py:{index}", "text": "t", "matchedTokens": []}

    @staticmethod
    def _mirror(path, mode):
        return {"candidateClass": "mirror", "matchMode": mode, "path": path,
                "line": 1, "endLine": 2, "site": f"{path}:1-2",
                "text": "copy", "fingerprint": "f"}

    def _apply(self, candidates, cap=20):
        result = sweeper.SweepResult(signature="s", label="cap", flagged=())
        sweeper._set_candidates(result, candidates, cap)
        return result

    def test_a_mirror_survives_a_flood_of_token_hits(self):
        """25 token hits in an alphabetically earlier file used to fill the cap
        and drop the exact copy entirely."""
        result = self._apply(
            [self._token(i) for i in range(1, 26)] + [self._mirror("z.py", "exact")]
        )

        assert result.truncated is True
        assert "z.py:1-2" in [c["site"] for c in result.candidates]

    def test_both_mirror_kinds_outrank_token_hits(self):
        result = self._apply(
            [self._token(i) for i in range(1, 26)]
            + [self._mirror("z.py", "exact"), self._mirror("m.py", "normalized")]
        )

        sites = [c["site"] for c in result.candidates]
        assert "z.py:1-2" in sites and "m.py:1-2" in sites

    def test_an_exact_mirror_outranks_a_normalized_one(self):
        candidates = [self._mirror("z.py", "exact"), self._mirror("m.py", "normalized")]
        result = self._apply(candidates, cap=1)

        assert [c["site"] for c in result.candidates] == ["z.py:1-2"]

    def test_reported_candidates_stay_in_location_order(self):
        """Strength decides what is kept, not how it reads."""
        result = self._apply(
            [self._token(i) for i in range(1, 26)] + [self._mirror("z.py", "exact")]
        )

        assert result.candidates == sorted(result.candidates, key=sweeper._location_key)

    def test_nothing_is_reordered_when_under_the_cap(self):
        candidates = [self._mirror("z.py", "exact"), self._token(1)]
        result = self._apply(candidates)

        assert result.truncated is False
        assert [c["site"] for c in result.candidates] == ["a.py:1", "z.py:1-2"]


class TestBracketDirectives:
    """Pyre writes its suppressions as a bracketed error code with no
    separator, so the `tool: setting` shape alone does not see them."""

    @pytest.mark.parametrize("comment", [
        "# pyre-ignore[7]",
        "# pyre-fixme[16]",
        "# pyre-ignore-all-errors[21]",
    ])
    def test_bracket_directives_are_content(self, comment):
        assert sweeper._is_python_directive(comment)

    @pytest.mark.parametrize("comment", [
        "# see [1] for details",
        "# returns [a, b] as a pair",
        "# handles [] gracefully",
    ])
    def test_prose_with_brackets_is_still_an_ordinary_comment(self, comment):
        """A space before the bracket is what separates the two: every Pyre
        form is written tight against it, English is not."""
        assert not sweeper._is_python_directive(comment)

    def test_differing_pyre_codes_are_not_duplicates(self, tmp_path):
        """The codes suppress different errors, so removing them would make
        two unrelated blocks normalize identically."""
        (tmp_path / "checked.py").write_text(
            "value = compute(payload)  # pyre-ignore[7]\n"
            "emit_value(value)\n"
            "spacer()\n"
            "value = compute(payload)  # pyre-ignore[16]\n"
            "emit_value(value)\n"
        )

        result = sweeper.sweep(
            signature="pyre", label="pyre", sites=["checked.py:1-2"],
            changed_files=["checked.py"], root=tmp_path,
        )

        assert result.candidates == []

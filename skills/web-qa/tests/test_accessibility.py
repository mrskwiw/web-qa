"""Accessibility audit — pure parse tests + a browser smoke test on a fixture.

The parse tests are hermetic (no browser). The CLI smoke test launches Chromium
against a local file:// fixture with one instance of each violation, and skips
(does not fail) when the browser binary isn't installed — matching
test_cli_smoke.py.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine.accessibility import parse_a11y
from engine.cli import cli
from engine.models import A11yImpact

_FIXTURES = Path(__file__).parent / "fixtures"
A11Y_FIXTURE = (_FIXTURES / "a11y.html").resolve()


# --- pure parse tests (no browser) -----------------------------------------


def test_parse_attaches_impact_help_and_counts():
    raw = {
        "violations": [
            {"rule": "duplicate-id", "count": 2, "nodes": [{"selector": "#dup"}]},
            {"rule": "image-alt", "count": 1, "nodes": [{"selector": "img"}]},
        ]
    }
    report = parse_a11y(raw, "https://e.com/")
    assert report.url == "https://e.com/"
    assert report.counts == {
        "critical": 0,
        "serious": 1,
        "moderate": 0,
        "minor": 1,
        "total": 2,
    }
    # serious (image-alt) sorts before minor (duplicate-id)
    assert [v.rule for v in report.violations] == ["image-alt", "duplicate-id"]
    img = report.violations[0]
    assert img.impact is A11yImpact.SERIOUS
    assert "alt" in img.help.lower()
    assert img.count == 1


def test_parse_skips_unknown_rules():
    report = parse_a11y({"violations": [{"rule": "not-a-real-rule"}]}, "https://e.com/")
    assert report.violations == []
    assert report.counts["total"] == 0


def test_parse_empty_is_clean():
    report = parse_a11y({}, "https://e.com/")
    assert report.violations == []
    assert report.counts["total"] == 0


# --- browser smoke test on the fixture -------------------------------------


def _run_a11y(url):
    res = CliRunner().invoke(cli, ["a11y", "--url", url])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"a11y failed: {msg}")
    return json.loads(res.output)


def test_a11y_fixture_flags_each_rule():
    report = _run_a11y(A11Y_FIXTURE.as_uri())
    rules = {v["rule"]: v for v in report["violations"]}

    # every seeded violation is detected
    for expected in (
        "html-has-lang",
        "document-title",
        "image-alt",
        "control-name",
        "button-name",
        "link-name",
        "positive-tabindex",
        "duplicate-id",
        "heading-order",
    ):
        assert expected in rules, f"expected {expected} to be flagged; got {list(rules)}"

    # and the NEGATIVE cases don't over-fire — the labelled / decorative / named
    # / default-labelled siblings stay clean:
    assert rules["image-alt"]["count"] == 1  # alt="" decorative img not flagged
    assert rules["control-name"]["count"] == 1  # #email (labelled) not flagged
    # button-name: icon <button> + input[type=button] (no value) + input[type=image]
    # (no alt). The aria-label button, submit (default label), valued button,
    # alt'd image button, and the button whose value is set at RUNTIME via the
    # live .value property (#dynbtn) are all clean — count stays 3, not 4.
    assert rules["button-name"]["count"] == 3
    # link-name: empty <a href> + empty role="link" span. "Home" and "Docs" clean.
    assert rules["link-name"]["count"] == 2

    # impact + help are attached
    assert rules["image-alt"]["impact"] == "serious"
    assert rules["duplicate-id"]["impact"] == "minor"
    assert rules["image-alt"]["nodes"], "a flagged rule should carry example nodes"


def test_a11y_embedded_in_explore_snapshot():
    res = CliRunner().invoke(cli, ["explore", "--url", A11Y_FIXTURE.as_uri()])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(msg)
    snap = json.loads(res.output)
    assert snap.get("accessibility"), "explore snapshot must embed an accessibility report"
    assert snap["accessibility"]["counts"]["total"] >= 8

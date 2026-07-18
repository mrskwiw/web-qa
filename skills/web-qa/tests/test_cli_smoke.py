"""CLI subcommand smoke tests against a local file:// fixture page (Phase F).

These launch a real Chromium via Playwright. If the browser binary isn't
installed (``python -m playwright install chromium``), they skip rather than
fail, so the pure test suite still runs anywhere.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine.cli import cli

FIXTURE = (Path(__file__).parent / "fixtures" / "form.html").resolve()


def _run_flow(tmp_path, steps):
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps(steps), encoding="utf-8")
    res = CliRunner().invoke(
        cli, ["flow", "--url", FIXTURE.as_uri(), "--steps", str(steps_file)]
    )
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"flow failed: {msg}")
    return json.loads(res.output)


def test_explore_smoke(tmp_path):
    res = CliRunner().invoke(cli, ["explore", "--url", FIXTURE.as_uri()])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(msg)
    snap = json.loads(res.output)
    assert snap["title"] == "web-qa flow fixture"
    # the form's true submit should resolve to the Save button, not the first button
    assert snap["forms"], "expected a form in the snapshot"
    # fixture form has no password + no destructive wording → not destructive
    assert snap["forms"][0]["destructive"] is False
    # incomplete-feature detection covers both a badge AND a marker buried in a
    # plain nested <div>/<p> (text-based scan, not a narrow element selector)
    markers = {m["marker"] for m in snap["incomplete"]}
    assert "coming soon" in markers
    assert "under construction" in markers
    # dead-link detection: the href="#" link is flagged, the real one is not
    by_text = {lk["text"]: lk for lk in snap["links"]}
    assert by_text["Settings"]["dead"] is True
    assert by_text["Dashboard"]["dead"] is False


def test_flow_passes_and_validates_each_step(tmp_path):
    data = _run_flow(
        tmp_path,
        [
            {"type": "fill", "selector": "#name", "value": "Ada", "label": "fill name"},
            {
                "type": "click",
                "selector": "#save",
                "label": "save",
                "assert": {"dom_contains": "Saved!"},
            },
        ],
    )
    assert data["metadata"]["steps_run"] == 2
    assert data["halted_at"] is None
    assert all(s["passed"] for s in data["steps"])


def test_flow_halts_on_failed_assertion(tmp_path):
    data = _run_flow(
        tmp_path,
        [
            {
                "type": "click",
                "selector": "#save",
                "label": "save",
                "assert": {"dom_contains": "Saved!"},
            },
            {
                "type": "click",
                "selector": "#noop",
                "label": "noop",
                "assert": {"dom_contains": "THIS WILL NEVER APPEAR"},
            },
        ],
    )
    assert data["metadata"]["halted"] is True
    assert data["halted_at"]["index"] == 1
    assert data["steps"][0]["passed"] is True
    assert data["steps"][1]["passed"] is False


def test_flow_select_option_by_label(tmp_path):
    # the `select` action drives a native <select> — required for pickers like
    # the Tool Library client dropdown; author by visible label ("Banana").
    data = _run_flow(
        tmp_path,
        [
            {
                "type": "select",
                "selector": "#fruit",
                "value": "Banana",
                "label": "pick fruit",
                "assert": {"dom_contains": "Picked banana"},
            }
        ],
    )
    assert data["metadata"]["steps_run"] == 1
    assert data["halted_at"] is None
    assert data["steps"][0]["passed"] is True


def test_flow_missing_secret_errors(tmp_path):
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(
        json.dumps(
            [{"type": "fill", "selector": "#name", "value": {"env": "NOPE_VAR"}}]
        ),
        encoding="utf-8",
    )
    res = CliRunner().invoke(
        cli, ["flow", "--url", FIXTURE.as_uri(), "--steps", str(steps_file)]
    )
    # a missing secret must fail loudly, not send an empty credential
    assert res.exit_code != 0
    assert "NOPE_VAR" in str(res.exception or res.output)

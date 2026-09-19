"""CLI subcommand smoke tests against a local file:// fixture page (Phase F).

These launch a real Chromium via Playwright. If the browser binary isn't
installed (``python -m playwright install chromium``), they skip rather than
fail, so the pure test suite still runs anywhere.
"""

import http.server
import json
import socketserver
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine.cli import cli

_FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = (_FIXTURES / "form.html").resolve()
HIDDEN_DUP_FIXTURE = (_FIXTURES / "dup_hidden.html").resolve()
LOGIN_WITH_SIGNUP_LINK_FIXTURE = (_FIXTURES / "login_with_signup_link.html").resolve()
LOGIN_WITH_SECOND_SUBMIT_FIXTURE = (_FIXTURES / "login_with_second_submit.html").resolve()


def _run_flow(tmp_path, steps, url=None):
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps(steps), encoding="utf-8")
    res = CliRunner().invoke(
        cli, ["flow", "--url", url or FIXTURE.as_uri(), "--steps", str(steps_file)]
    )
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"flow failed: {msg}")
    return json.loads(res.output)


def test_act_emits_gated_bundle(tmp_path):
    """The `act` subcommand drives one action and emits a §5 evidence bundle with a
    computed gate — the primary 'agent's hands' command, previously untested."""
    action = json.dumps({"type": "scroll", "inferred_intent": "scroll the page"})
    res = CliRunner().invoke(cli, ["act", "--url", FIXTURE.as_uri(), "--action", action])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(msg)
    bundle = json.loads(res.output)
    # full bundle shape + a real gate verdict (a plain scroll is objectively clean)
    assert bundle["action"]["type"] == "scroll"
    assert {"url_before", "url_after", "gate", "http", "console"} <= set(bundle)
    assert bundle["gate"]["passed"] is True
    names = {c["name"] for c in bundle["gate"]["checks"]}
    assert {"no_console_errors", "http_status_ok", "no_crash"} <= names


def test_flow_save_session_round_trip(tmp_path):
    """`flow --save-session` persists a replayable session bundle (cookies +
    storage_state + pinned user-agent) — the establish-once-replay primitive
    (browser.save_session), previously untested."""
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps([{"type": "scroll", "label": "scroll"}]), encoding="utf-8")
    sess = tmp_path / "session.json"
    res = CliRunner().invoke(cli, [
        "flow", "--url", FIXTURE.as_uri(), "--steps", str(steps_file),
        "--save-session", str(sess), "--user-agent", "QA-UA/1.0",
    ])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(msg)
    assert sess.exists()
    bundle = json.loads(sess.read_text(encoding="utf-8"))
    assert bundle["user_agent"] == "QA-UA/1.0"
    assert "storage_state" in bundle  # cookies + localStorage container
    assert json.loads(res.output)["metadata"]["session_saved"]


def test_flow_refuses_a_destructive_run_without_yes(tmp_path):
    """`--destructive` is a per-flow risk self-declaration: the session's own Bash
    permission prompt is the outer backstop, but a human skimming a raw steps.json
    for a real delete buried in step 6 can miss it. Refuses (nothing launched)
    without --yes."""
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(
        json.dumps([{"type": "click", "selector": "#save", "label": "save"}]),
        encoding="utf-8",
    )
    res = CliRunner().invoke(cli, [
        "flow", "--url", FIXTURE.as_uri(), "--steps", str(steps_file), "--destructive",
    ])
    assert res.exit_code == 0, res.output  # web-qa's CLI never uses exit codes for signaling
    data = json.loads(res.output)
    assert data["metadata"]["refused"] is True
    assert "destructive" in data["metadata"]["reason"]
    assert data["metadata"]["steps_run"] == 0
    assert data["steps"] == []


def test_flow_runs_a_destructive_run_with_yes(tmp_path):
    data = _run_flow_with_flags(
        tmp_path,
        [{"type": "click", "selector": "#save", "label": "save", "assert": {"dom_contains": "Saved!"}}],
        ["--destructive", "--yes"],
    )
    assert data["metadata"]["refused"] is False
    assert data["metadata"]["steps_run"] == 1
    assert data["steps"][0]["passed"] is True


def test_flow_refuses_a_costed_run_without_yes(tmp_path):
    """`--costs` is `--destructive`'s sibling gate: a flow that spends real credits
    or money without being destructive (a paid research call) was otherwise
    ungated entirely."""
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(
        json.dumps([{"type": "click", "selector": "#save", "label": "save"}]),
        encoding="utf-8",
    )
    res = CliRunner().invoke(cli, [
        "flow", "--url", FIXTURE.as_uri(), "--steps", str(steps_file), "--costs",
    ])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["metadata"]["refused"] is True
    assert "costed" in data["metadata"]["reason"]
    assert data["steps"] == []


def test_flow_runs_a_costed_run_with_yes(tmp_path):
    data = _run_flow_with_flags(
        tmp_path,
        [{"type": "click", "selector": "#save", "label": "save", "assert": {"dom_contains": "Saved!"}}],
        ["--costs", "--yes"],
    )
    assert data["metadata"]["refused"] is False
    assert data["steps"][0]["passed"] is True


def _run_flow_with_flags(tmp_path, steps, extra_flags):
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps(steps), encoding="utf-8")
    res = CliRunner().invoke(
        cli, ["flow", "--url", FIXTURE.as_uri(), "--steps", str(steps_file), *extra_flags]
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
    # duplicate controls are PRESERVED (not collapsed to one) AND each is uniquely
    # addressable — the three identical row-level "Edit" buttons share a base
    # selector, so each gets a Playwright `>> nth=` disambiguator.
    edits = [e for e in snap["interactive"] if e["text"] == "Edit"]
    assert len(edits) == 3, "repeated row controls must not be deduped away"
    sels = [e["selector"] for e in edits]
    assert len(set(sels)) == 3, "each duplicate must be uniquely addressable"
    assert all(
        "nth=" in s for s in sels
    ), "non-unique selectors need an nth disambiguator"


def test_explore_does_not_misclassify_a_login_form_with_a_signup_cross_link():
    """BUGS.md 2026-09-16/18: a login form's own text includes a nested "Don't
    have an account? Sign up" cross-link inside the SAME <form> -- "sign up"
    alone used to satisfy the whole-form DESTRUCTIVE regex and misclassify an
    ordinary, idempotent login as destructive. The submit itself says "Sign
    in", so it must now be exempt regardless of the cross-link. A second,
    genuinely destructive form on the same page proves the fix does not widen
    into a blanket exemption -- only a login-shaped SUBMIT is spared."""
    res = CliRunner().invoke(
        cli, ["explore", "--url", LOGIN_WITH_SIGNUP_LINK_FIXTURE.as_uri()]
    )
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(msg)
    snap = json.loads(res.output)
    # submit selectors are CSS, not text -- resolve each form by its fields instead
    login_form = next(
        f for f in snap["forms"] if any(x["name"] == "password" for x in f["fields"])
    )
    assert login_form["destructive"] is False, (
        "login form misclassified destructive by its own signup cross-link"
    )
    delete_form = next(
        f for f in snap["forms"] if any(x["name"] == "confirm" for x in f["fields"])
    )
    assert delete_form["destructive"] is True, (
        "genuinely destructive form must still be flagged -- the fix must not "
        "widen into a blanket exemption"
    )


def test_explore_does_not_exempt_a_second_destructive_submit_in_the_same_form():
    """Post-commit Codex review (2026-09-18) of the login-exemption fix above:
    `submitEl` resolves to only ONE winner by priority order, so a single
    <form> with TWO submit buttons ("Sign in" AND "Delete account") must not
    have the second action's risk hidden just because the priority chain
    happened to pick the login one. The exemption is gated on exactly one
    submit-capable control existing in the form -- with two, this form must
    still come out destructive."""
    res = CliRunner().invoke(
        cli, ["explore", "--url", LOGIN_WITH_SECOND_SUBMIT_FIXTURE.as_uri()]
    )
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(msg)
    snap = json.loads(res.output)
    assert snap["forms"][0]["destructive"] is True, (
        "a form with a second, genuinely destructive submit must not be "
        "exempted just because the resolved submitEl says 'Sign in'"
    )


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


def test_flow_clicks_disambiguated_duplicate(tmp_path):
    # The three identical "Edit" buttons collapse to one base selector; the
    # snapshot hands back `button.row-edit >> nth=N`. Clicking nth=1 must hit the
    # SECOND row (Row B), proving the disambiguator addresses the right element
    # rather than always firing the first match.
    data = _run_flow(
        tmp_path,
        [
            {
                "type": "click",
                "selector": "button.row-edit >> nth=1",
                "label": "edit row B",
                "assert": {"dom_contains": "Edit B"},
            }
        ],
    )
    assert data["halted_at"] is None
    assert data["steps"][0]["passed"] is True


def test_nth_disambiguator_skips_hidden_duplicate(tmp_path):
    # A hidden clone sits FIRST in DOM order, before the three visible rows. The
    # snapshot must (a) inventory only the 3 visible Edit buttons, and (b) emit
    # nth selectors that each click their OWN visible row — proving nth is indexed
    # against the same full DOM set Playwright resolves against (hidden node
    # included), not the visible-only subset. Guards the review's high finding.
    res = CliRunner().invoke(cli, ["explore", "--url", HIDDEN_DUP_FIXTURE.as_uri()])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(msg)
    edits = [e for e in json.loads(res.output)["interactive"] if e["text"] == "Edit"]
    assert len(edits) == 3, "hidden clone must be filtered out of the inventory"
    # Each emitted selector must click its own visible row (A, B, C in DOM order),
    # never the hidden clone or the wrong row.
    for elem, want in zip(edits, ["Edit A", "Edit B", "Edit C"]):
        data = _run_flow(
            tmp_path,
            [
                {
                    "type": "click",
                    "selector": elem["selector"],
                    "label": want,
                    "assert": {"dom_contains": want},
                }
            ],
            url=HIDDEN_DUP_FIXTURE.as_uri(),
        )
        assert (
            data["steps"][0]["passed"] is True
        ), f"{elem['selector']} should click {want}"


@contextmanager
def _busy_server():
    """Serve a page whose script fires a repeating fetch, so the browser never
    reaches networkidle — the persistent-connection case (polling/websockets/SSE)
    that a ``networkidle`` navigation would hang on."""
    html = (
        b"<!doctype html><title>busy</title><h1>Busy</h1>"
        b"<script>setInterval(function(){fetch('/ping').catch(function(){});},150);</script>"
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib callback name
            body = b"pong" if self.path == "/ping" else html
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence per-request logging
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}/"
    finally:
        srv.shutdown()
        srv.server_close()


def test_navigate_survives_never_idle_page():
    # A page that never reaches networkidle must still be captured (bounded
    # settle), not time out. Guards against regressing navigate() back to
    # goto(wait_until="networkidle").
    with _busy_server() as url:
        res = CliRunner().invoke(cli, ["explore", "--url", url])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(msg)
    snap = json.loads(res.output)
    assert snap["title"] == "busy"


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

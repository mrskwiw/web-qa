"""Core `browser.py` coverage: session replay, popups, late APIs, action dispatch.

These are the highest-risk paths in the engine and were the least directly
tested (audit IDs T2/T4/T7, `docs/COMPLETION_AND_OPTIMIZATION_PLAN.md`). They
cannot be exercised against a ``file://`` fixture: session replay needs cookies,
``settle_popups`` needs a real document status for an opened tab, and
``wait_for_api`` needs a response that lands *after* the post-action settle. So
each test runs against a local stdlib HTTP server.

The two assertions that matter most, because a regression in either fails
silently rather than loudly:

* **Session replay** — that a saved bundle actually authenticates a *second*
  run. Asserting the file merely exists (as the previous test did) would still
  pass if cookies were dropped or the bundle were ignored on load. Paired here
  with a no-session control, so the cookie is proven to be what did it.
* **Late-landing API capture** — that ``await_response`` is what pulls a slow
  call into the step's evidence, proven by a control step without it.

They launch a real Chromium and skip (never fail) when the browser binary is
absent, matching `test_cli_smoke.py`.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine.cli import cli

SESSION_COOKIE = "sid=web-qa-test-session"
SECRET = "SECRET-DASHBOARD-OK"
SLOW_API_MS = 1200  # comfortably longer than perform()'s 300ms post-action settle

PAGE = """<!doctype html>
<title>core fixture</title>
<h1>Core fixture</h1>
<button id="go" onclick="fetch('/slow-api').then(r=>r.text()).then(t=>{
  document.getElementById('out').textContent = t;
})">Run</button>
<div id="out">idle</div>
<button id="batch" onclick="
  [1200,2600,4200].forEach(function(d,i){setTimeout(function(){fetch('/api/b'+i);},d);});
">Run batch</button>
<a id="newtab" href="/private" target="_blank">Open dashboard</a>
<a id="broken" href="/missing" target="_blank">Open missing</a>
<input id="name" name="name" onkeydown="if(event.key==='Enter'){
  document.getElementById('out').textContent='ENTER-PRESSED:'+this.value;
}">
""".encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    """Routes: /login sets an auth cookie, /private requires it, /slow-api is late."""

    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, headers=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — stdlib callback name
        if self.path == "/login":
            self._send(
                200,
                b"<title>login</title><h1>Logged in</h1>",
                headers=[("Set-Cookie", f"{SESSION_COOKIE}; Path=/")],
            )
        elif self.path == "/private":
            if SESSION_COOKIE in (self.headers.get("Cookie") or ""):
                # Echo the received User-Agent so a replay can prove the session
                # bundle's pinned UA was actually applied, not just its cookies.
                ua = self.headers.get("User-Agent") or "?"
                self._send(
                    200,
                    f"<title>dash</title><h1>{SECRET}</h1><p>UA:{ua}</p>".encode(),
                )
            else:
                self._send(401, b"<title>denied</title><h1>PLEASE-LOG-IN</h1>")
        elif self.path == "/slow-api":
            time.sleep(SLOW_API_MS / 1000)
            self._send(200, b"slow-api-done")
        elif self.path == "/redirector":
            # Navigates itself immediately, so a capture started here races a
            # live navigation — the condition that used to crash the engine.
            self._send(
                200,
                b"<!doctype html><title>redirector</title>"
                b"<script>location.replace('/private');</script><h1>going</h1>",
            )
        elif self.path.startswith("/api/b"):
            self._send(200, b"batch-ok")
        elif self.path == "/missing":
            self._send(404, b"<title>404</title><h1>Not Found</h1>")
        else:
            self._send(200, PAGE)

    def log_message(self, *args):  # silence per-request logging
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Swallow benign client disconnects.

        With HTTP/1.1 keep-alive, Chromium routinely drops a pooled connection
        when the context closes. socketserver's default handler prints that
        traceback to *stdout* — which CliRunner captures into ``res.output``,
        corrupting the JSON these tests parse. Silence is correct here: a real
        handler bug still surfaces as a failed assertion on the response.
        """


@contextmanager
def _server():
    srv = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _invoke(args):
    """Run the CLI, skipping (not failing) when Chromium isn't installed."""
    res = CliRunner().invoke(cli, args)
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"{args[0]} failed (exit {res.exit_code}): {msg}")
    if not res.output.strip():
        raise AssertionError(
            f"{args[0]} exited 0 but produced no output (exception={res.exception!r})"
        )
    try:
        return json.loads(res.output)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{args[0]} emitted non-JSON output: {exc}\n--- first 400 chars ---\n"
            f"{res.output[:400]!r}"
        ) from exc


def _steps(tmp_path: Path, steps, name="steps.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(steps), encoding="utf-8")
    return str(path)


# -- session persistence ---------------------------------------------------


def test_saved_session_authenticates_a_later_run(tmp_path):
    """establish-once-replay: a bundle saved by one run authenticates the NEXT one.

    The control half is the point — the same request without ``--session`` must
    be rejected. Without it, a test could pass on a route that was never actually
    protected, proving nothing about the cookie.
    """
    sess = tmp_path / "session.json"
    with _server() as base:
        # 1. Establish: visit /login (sets the cookie), persist the context.
        _invoke(
            [
                "flow",
                "--url",
                f"{base}/login",
                "--steps",
                _steps(tmp_path, [{"type": "scroll", "label": "settle"}]),
                "--save-session",
                str(sess),
                "--user-agent",
                "QA-UA/1.0",
            ]
        )
        bundle = json.loads(sess.read_text(encoding="utf-8"))
        assert bundle["user_agent"] == "QA-UA/1.0"
        cookies = bundle["storage_state"].get("cookies", [])
        assert any(c["name"] == "sid" for c in cookies), "auth cookie not persisted"

        # 2. Replay: the saved session must reach the protected route.
        # `content_contains`, not `dom_contains`: these fixture pages are plain
        # text, and the DOM outline is a role/text tree of interactive+landmark
        # nodes — legitimately empty here. Rendered content is the right net.
        replayed = _invoke(
            [
                "flow",
                "--url",
                f"{base}/private",
                "--steps",
                _steps(
                    tmp_path,
                    [
                        {
                            "type": "scroll",
                            "label": "read",
                            "assert": {"content_contains": SECRET},
                        }
                    ],
                    "replay.json",
                ),
                "--session",
                str(sess),
                # NO --user-agent here on purpose: the UA must be inherited from
                # the bundle. Passing it would mask a regression in which
                # save/load stopped persisting or applying user_agent, and UA is
                # half of the fingerprint a bound token is validated against.
            ]
        )
        assert (
            replayed["steps"][0]["passed"] is True
        ), "replayed session was not authenticated"
        assert "UA:QA-UA/1.0" in replayed["steps"][0]["bundle"]["content_after"], (
            "the replayed run did not present the session bundle's pinned "
            "user-agent — a UA+IP-fingerprint-bound token would be rejected"
        )

        # 3. Control: without the session the SAME route is denied — so step 2
        #    passed because of the replayed cookie, not because /private is open.
        anon = _invoke(
            [
                "flow",
                "--url",
                f"{base}/private",
                "--steps",
                _steps(
                    tmp_path,
                    [
                        {
                            "type": "scroll",
                            "label": "read",
                            "assert": {"content_contains": SECRET},
                        }
                    ],
                    "anon.json",
                ),
            ]
        )
        assert anon["steps"][0]["passed"] is False
        assert "PLEASE-LOG-IN" in anon["steps"][0]["bundle"]["content_after"]


# -- late-landing API capture (wait_for_api) --------------------------------


def test_await_response_captures_a_late_api_call(tmp_path):
    """``await_response`` polls captured network so a slow call lands in the step's
    evidence. The no-await control proves the wait is what captured it, not luck."""
    with _server() as base:
        awaited = _invoke(
            [
                "flow",
                "--url",
                base,
                "--steps",
                _steps(
                    tmp_path,
                    [
                        {
                            "type": "click",
                            "selector": "#go",
                            "label": "run",
                            "await_response": {
                                "path_contains": "/slow-api",
                                "timeout_ms": 8000,
                            },
                        }
                    ],
                ),
            ]
        )
        urls = [c["url"] for c in awaited["steps"][0]["bundle"]["http"]]
        assert any(
            "/slow-api" in u for u in urls
        ), "awaited response missing from evidence"

        # Control: the same click without await_response captures the after-state
        # while the request is still in flight, so the call is NOT in the delta.
        plain = _invoke(
            [
                "flow",
                "--url",
                base,
                "--steps",
                _steps(
                    tmp_path,
                    [{"type": "click", "selector": "#go", "label": "run"}],
                    "plain.json",
                ),
            ]
        )
        plain_urls = [c["url"] for c in plain["steps"][0]["bundle"]["http"]]
        assert not any("/slow-api" in u for u in plain_urls), (
            "control captured the late call anyway — the timing margin is too thin "
            "for this test to prove await_response does anything"
        )


def test_await_response_returns_false_on_timeout(tmp_path):
    """A never-arriving response must time out and let the flow proceed, not hang."""
    with _server() as base:
        data = _invoke(
            [
                "flow",
                "--url",
                base,
                "--steps",
                _steps(
                    tmp_path,
                    [
                        {
                            "type": "click",
                            "selector": "#go",
                            "label": "run",
                            "await_response": {
                                "path_contains": "/never-fires",
                                "timeout_ms": 1000,
                            },
                        }
                    ],
                ),
            ]
        )
        assert data["metadata"]["steps_run"] == 1  # proceeded rather than hanging


def test_settle_window_captures_a_client_driven_xhr_batch(tmp_path):
    """Every XHR a client-side batch fires during ``settle_ms`` lands in the step's
    http delta — the response listener samples the WHOLE window, it does not stop
    at the action.

    This is the regression guard for BUGS.md C5, which was filed claiming late
    XHRs "aren't captured in the step's http window" and that the fix required
    re-architecting the listener. That diagnosis was wrong: capture already works
    across the window (asserted below). The real constraint is that the window
    must be declared up front — see the C5 entry for the corrected analysis.

    Two deliberate timing choices, both to stop this passing for the wrong reason:

    * The batch starts at **+1200ms**, well clear of the unconditional 300ms
      post-action wait in ``perform()``. An earlier first request would leave the
      negative control with ~100ms of slack, so it would flap on a loaded host —
      passing not because the window was absent but because the machine was slow.
    * The last request fires at **+4200ms** against a 5000ms window, so the test
      probes near the boundary. With every request bunched early, a regression
      that truncated the settle to ~3s would still pass and the "whole window"
      claim would be untested.
    """
    with _server() as base:
        covered = _invoke(
            [
                "flow",
                "--url",
                base,
                "--steps",
                _steps(
                    tmp_path,
                    [
                        {
                            "type": "click",
                            "selector": "#batch",
                            "label": "batch",
                            "settle_ms": 5000,  # spans all three staggered fetches
                        }
                    ],
                ),
            ]
        )
        urls = [c["url"] for c in covered["steps"][0]["bundle"]["http"]]
        for i in range(3):
            assert any(
                f"/api/b{i}" in u for u in urls
            ), f"XHR /api/b{i} fired during the settle window but was not captured"

        # Control: with no settle window the after-state is captured while the
        # batch is still pending, so none of them are in the delta. This is the
        # behaviour that made C5 look like a capture bug.
        uncovered = _invoke(
            [
                "flow",
                "--url",
                base,
                "--steps",
                _steps(
                    tmp_path,
                    [{"type": "click", "selector": "#batch", "label": "batch"}],
                    "nosettle.json",
                ),
            ]
        )
        plain = [c["url"] for c in uncovered["steps"][0]["bundle"]["http"]]
        assert not any("/api/b" in u for u in plain)


# -- popups / opened tabs (settle_popups) -----------------------------------


def test_new_tab_click_records_opened_page_status(tmp_path):
    """A target=_blank click is followed and its document status recorded — the
    only way a broken external link is caught (spec section 6, opened_pages_ok)."""
    with _server() as base:
        ok = _invoke(
            [
                "act",
                "--url",
                base,
                "--action",
                json.dumps({"type": "click", "selector": "#newtab"}),
            ]
        )
        assert ok["opened"], "opened tab was not captured"
        assert ok["opened"][0]["status"] == 401  # unauthenticated, but reached

        broken = _invoke(
            [
                "act",
                "--url",
                base,
                "--action",
                json.dumps({"type": "click", "selector": "#broken"}),
            ]
        )
        assert broken["opened"][0]["status"] == 404
        checks = {c["name"]: c for c in broken["gate"]["checks"]}
        assert (
            checks["opened_pages_ok"]["passed"] is False
        ), "a new tab landing on 404 must fail the gate — this is the broken-link check"
        assert broken["gate"]["passed"] is False


# -- action dispatch --------------------------------------------------------


def test_act_with_selector_populates_target_present(tmp_path):
    """A selector-bearing action records whether the target survived. A
    selector-less action (scroll) leaves it null, so only this shape covers the
    is_present path that feeds the target_survived advisory check."""
    with _server() as base:
        clicked = _invoke(
            [
                "act",
                "--url",
                base,
                "--action",
                json.dumps({"type": "click", "selector": "#go"}),
            ]
        )
        assert clicked["target_present"] is True
        assert clicked["action"]["selector"] == "#go"

        scrolled = _invoke(
            [
                "act",
                "--url",
                base,
                "--action",
                json.dumps({"type": "scroll"}),
            ]
        )
        assert scrolled["target_present"] is None


def test_press_and_navigate_dispatch(tmp_path):
    """`press` and `navigate` are the two dispatch branches no other test drives."""
    with _server() as base:
        data = _invoke(
            [
                "flow",
                "--url",
                base,
                "--steps",
                _steps(
                    tmp_path,
                    [
                        {
                            "type": "fill",
                            "selector": "#name",
                            "value": "Ada",
                            "label": "fill",
                        },
                        {
                            "type": "press",
                            "selector": "#name",
                            "key": "Enter",
                            "label": "press",
                            # The keypress must have an OBSERVABLE effect asserted
                            # on the press step itself. Without this the branch
                            # could be a no-op, or target the wrong element, and
                            # the flow would still pass on the later steps.
                            "assert": {"content_contains": "ENTER-PRESSED:Ada"},
                        },
                        {
                            "type": "navigate",
                            "url": f"{base}/login",
                            "label": "navigate",
                            "assert": {"content_contains": "Logged in"},
                        },
                    ],
                ),
            ]
        )
        assert data["metadata"]["steps_run"] == 3
        assert all(s["passed"] for s in data["steps"])
        assert data["steps"][2]["bundle"]["url_after"].endswith("/login")


# -- capture across an in-flight navigation ---------------------------------


def test_capture_is_internally_consistent_when_the_page_navigates(tmp_path):
    """A snapshot must describe ONE document, never a mix of two.

    The fixture's /redirector sends the browser to /private the moment it loads,
    so capture races a live navigation. Two distinct failures are guarded here:

    * the old crash ("Execution context was destroyed") — `act` used to exit
      non-zero with no bundle at all, which made every OAuth/SSO/payment
      hand-off untestable;
    * the subtler torn read — url/title/content fetched in separate awaits could
      come from different documents, producing a bundle that looks fine while
      describing the wrong page, so the gate judges the wrong thing.

    Asserting *consistency* rather than a specific destination is deliberate:
    either document is a legitimate outcome depending on timing, but a snapshot
    that claims one URL while carrying the other's content never is.
    """
    with _server() as base:
        bundle = _invoke(
            [
                "act",
                "--url",
                f"{base}/redirector",
                "--action",
                json.dumps({"type": "scroll", "inferred_intent": "capture mid-nav"}),
            ]
        )

    url, content = bundle["url_after"], bundle["content_after"]
    if "/private" in url:
        assert (
            "PLEASE-LOG-IN" in content or SECRET in content
        ), f"url says /private but content does not match it: {content[:120]!r}"
    else:
        assert (
            "redirector" in url or "/private" not in content
        ), f"snapshot mixes documents: url={url!r} content={content[:120]!r}"
    assert bundle["gate"] is not None, "no gate computed — capture bailed out"

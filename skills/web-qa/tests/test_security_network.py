"""Security-sweep NETWORK path — the auth-bypass probe against a live local server.

The pure logic (enumerate/classify/filter/dedup) is covered in ``test_security.py``.
This exercises the part that actually touches the network and was previously
unexecuted: ``security.probe``/``_status``/``sweep`` and the ``cli sweep`` command,
including ``--include-mutating`` gating and the PARTIAL-coverage disclosure. It uses
stdlib ``http.server`` (no Playwright), so it runs everywhere.
"""

import http.server
import json
import socketserver
import threading
from contextlib import contextmanager

from click.testing import CliRunner

from engine import security
from engine.cli import cli
from engine.security import load_openapi

# OpenAPI surface the sweep enumerates. Mix of: an unauthenticated data exposure,
# a correctly-protected endpoint, a genuinely-public one, and a sensitive mutating
# route (only probed with --include-mutating).
_OPENAPI = {
    "paths": {
        "/api/open/list": {"get": {}},  # BUG: returns 200 with no token -> flagged
        "/api/clients/": {"get": {}},  # protected: 401 without token -> not flagged
        "/api/health": {"get": {}},  # public path -> not flagged
        "/api/health/cache/clear": {"post": {}},  # sensitive write -> only if mutating
    }
}


class _Handler(http.server.BaseHTTPRequestHandler):
    """A tiny target: /api/clients/ requires a bearer token; everything else is open."""

    def _reply(self):
        authed = self.headers.get("Authorization", "").startswith("Bearer ")
        if self.path.startswith("/api/clients/") and not authed:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"{}")
        elif self.path == "/openapi.json":
            # Serves the real spec, not the generic {} every other path returns,
            # so a test can assert load_openapi actually parsed THIS content.
            body = json.dumps(_OPENAPI).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    def do_GET(self):  # noqa: N802 (http.server API)
        self._reply()

    def do_POST(self):  # noqa: N802
        self._reply()

    def log_message(self, *_args):  # silence the test server
        return


@contextmanager
def _server():
    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as httpd:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            host, port = httpd.server_address
            yield f"http://{host}:{port}"
        finally:
            httpd.shutdown()


def test_sweep_flags_unauthenticated_exposure_only():
    """Safe (read-only) sweep flags the open GET, spares the protected + public ones,
    withholds the write verb, and labels coverage PARTIAL."""
    with _server() as base:
        result = security.sweep(base, _OPENAPI, token="valid-token")

    flagged = {(f.method, f.path) for f in result.flagged}
    assert ("GET", "/api/open/list") in flagged  # the real exposure
    assert ("GET", "/api/clients/") not in flagged  # 401 without token → correct
    assert ("GET", "/api/health") not in flagged  # public
    # the sensitive POST was withheld (safe default), so it can't be flagged here
    assert ("POST", "/api/health/cache/clear") not in flagged
    assert result.skipped_mutating == 1
    assert result.coverage.startswith("PARTIAL")
    assert result.include_mutating is False


def test_sweep_include_mutating_probes_write_and_flags_critical():
    """With --include-mutating the sensitive open POST is probed and flagged critical,
    and coverage is no longer PARTIAL."""
    with _server() as base:
        result = security.sweep(
            base, _OPENAPI, token="valid-token", include_mutating=True
        )

    crit = [f for f in result.flagged if f.path == "/api/health/cache/clear"]
    assert crit and crit[0].severity == "critical"
    assert result.skipped_mutating == 0
    assert result.coverage.startswith("complete")


def test_probe_returns_both_statuses():
    """probe() returns (no_token, with_token): the protected route rejects anon and
    accepts the token — the authenticated baseline the classifier reads for context."""
    with _server() as base:
        no_tok, with_tok = security.probe(base, "GET", "/api/clients/", "valid-token")
    assert no_tok == 401
    assert with_tok == 200


def test_load_openapi_fetches_from_a_url():
    """The `https?://` branch (`security.py:273-277`) — only the local-file and
    default-`<base>/openapi.json` branches had coverage before this. Asserts
    real content came back over the wire, not just that no exception fired."""
    with _server() as base:
        spec = load_openapi(f"{base}/openapi.json", base)
    assert spec == _OPENAPI


def test_cli_sweep_command_end_to_end(tmp_path):
    """Cover the `cli sweep` command body + load_openapi(file path)."""
    spec_file = tmp_path / "openapi.json"
    spec_file.write_text(json.dumps(_OPENAPI), encoding="utf-8")
    with _server() as base:
        res = CliRunner().invoke(
            cli, ["sweep", "--url", base, "--openapi", str(spec_file)]
        )
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["base_url"] == base
    assert any(f["path"] == "/api/open/list" for f in out["flagged"])
    assert out["coverage"].startswith("PARTIAL")

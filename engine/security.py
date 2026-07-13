"""Endpoint auth-enforcement sweep (Phase H, spec §6.1).

The one HTTP-level check in the engine: enumerate an app's API surface and
verify every endpoint that should be protected actually rejects unauthenticated
access. All *functional* testing stays browser-driven; auth enforcement is an
API-boundary property the UI cannot express.

Pure pieces (unit-testable, no network):

- :func:`enumerate_endpoints` — OpenAPI dict → list of (method, path).
- :func:`substitute_params` — fill ``{id}`` path params with a dummy.
- :func:`is_public` — default allowlist of endpoints allowed to be open.
- :func:`classify` — given the no-token / with-token statuses, decide whether an
  endpoint is a finding and at what severity.

The network probing (:func:`probe`, :func:`sweep`) uses only the stdlib.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import Serializable

_METHODS = ("get", "post", "put", "patch", "delete")

# Endpoints legitimately reachable without a token — never flagged.
_PUBLIC_EXACT = {
    "/",
    "/health",
    "/favicon.ico",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/api/health",
    "/api/health/live",
    "/api/health/ready",
}
# Prefixes whose endpoints are stateless public utilities (no data, no PII):
# calculators and pricing/config catalogs commonly answer unauthenticated.
_PUBLIC_PREFIXES = (
    "/api/auth/",  # login / register / refresh
    "/api/stripe/webhook",  # signature-gated, intentionally tokenless
    "/api/pricing/",  # stateless price calculators + public config
)
# Individually-public reference/catalog endpoints (a whole prefix would be too
# broad — e.g. /api/credits/{balance,transactions,purchase} MUST stay protected).
_PUBLIC_EXTRA = {
    "/api/credits/estimate",  # stateless credit calculator
    "/api/credits/packages",  # public price catalog
    "/api/generator/templates",  # public template catalog
    "/api/trends/categories",  # public reference lists
    "/api/trends/timeframes",
}
# Path fragments that make an unauthenticated success extra-serious.
_SENSITIVE = (
    "admin",
    "database",
    "privacy",
    "profiling",
    "credits/admin",
    "credits/grant",
    "cache",
    "debug",
    "internal",
)
# Fragments implying user/private data — never treat as public even under an
# allowlisted prefix, so a sensitive sibling (e.g. /api/pricing/history) can't
# slip through a broad prefix allow as a false negative.
_SENSITIVE_OVERRIDE = (
    "history",
    "/me",
    "/mine",
    "account",
    "export",
    "download",
    "private",
    "secret",
    "credential",
    "/user",
    "session",
)
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def enumerate_endpoints(openapi: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Flatten an OpenAPI ``paths`` object into sorted ``(METHOD, path)`` pairs."""
    out: List[Tuple[str, str]] = []
    for path, node in (openapi.get("paths") or {}).items():
        if not isinstance(node, dict):
            continue
        for method in node:
            if method.lower() in _METHODS:
                out.append((method.upper(), path))
    return sorted(set(out))


def substitute_params(path: str, dummy: str = "test-id-0") -> str:
    """Replace ``{param}`` templates with a dummy value for probing."""
    return re.sub(r"\{[^}]+\}", dummy, path)


def is_public(path: str) -> bool:
    """Whether an endpoint is allowed to answer unauthenticated.

    Public only if it is on the allowlist AND does not look sensitive: the
    fragment guard stops a private sibling under a broad prefix (e.g.
    ``/api/pricing/history``, ``/api/auth/me``) from being silently treated as
    public — a false negative erring toward *flagging for review*, which is the
    safe direction for a security check.
    """
    low = path.lower()
    if any(s in low for s in _SENSITIVE_OVERRIDE):
        return False
    if path in _PUBLIC_EXACT or path in _PUBLIC_EXTRA:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def filter_probeable(
    endpoints: List[Tuple[str, str]], include_mutating: bool
) -> Tuple[List[Tuple[str, str]], int]:
    """Split endpoints into (to-probe, count-of-skipped-mutating).

    Safe by default: probing a mutating verb (POST/PUT/PATCH/DELETE) sends a real
    request that WOULD execute on an unprotected endpoint — exactly the exposure
    the sweep hunts — so a ``DELETE`` or ``cache/clear`` could destroy/alter data
    on a non-test target. They are skipped unless ``include_mutating`` is set.
    The skipped count is disclosed in the result (no silent truncation).
    """
    if include_mutating:
        return list(endpoints), 0
    keep = [(m, p) for (m, p) in endpoints if m.upper() not in _MUTATING]
    return keep, len(endpoints) - len(keep)


def classify(
    method: str, path: str, status_no_token: int, status_with_token: Optional[int]
) -> Optional[Dict[str, str]]:
    """Decide whether an endpoint is a finding.

    Returns ``None`` for a pass (public, or correctly rejecting unauthenticated
    access with 401/403, or a 404/422 that never exposed anything). Otherwise a
    dict ``{severity, reason}``.

    Rule: a non-public endpoint is flagged when the **no-token** call is not
    rejected — i.e. it returned a success (2xx) or ran to a 5xx (meaning it
    executed past any auth check). 401/403/404/405 without a token are fine;
    422 (body validation) is ambiguous and left to the agent (not flagged here).
    ``status_with_token`` is recorded for context only; the verdict depends
    solely on the no-token response.
    """
    if is_public(path):
        return None
    rejected = status_no_token in (401, 403, 404, 405)
    if rejected:
        return None
    exposed = 200 <= status_no_token < 300 or status_no_token >= 500
    if not exposed:
        return None  # e.g. 422 validation — ambiguous, don't flag

    sensitive = any(s in path.lower() for s in _SENSITIVE)
    mutating = method.upper() in _MUTATING
    # Verb alone does NOT prove state mutation — many POST endpoints are stateless
    # calculators. Reserve critical/high for the sensitive groups (admin, database,
    # privacy, profiling, cache, credit-admin); everything else exposed lands in
    # the "medium / review" tier rather than crying wolf.
    if sensitive and mutating:
        severity = "critical"
    elif sensitive:
        severity = "high"
    else:
        severity = "medium"
    kind = "executed" if status_no_token >= 500 else f"returned {status_no_token}"
    verb = "state-changing" if mutating else "data/exposing"
    return {
        "severity": severity,
        "reason": f"{verb} endpoint {kind} with no token"
        + (" (sensitive group)" if sensitive else ""),
    }


def find_duplicate_exposures(findings: List["Finding"]) -> List[str]:
    """Heuristic: a protected action re-exposed under a second open path.

    Groups by the last two path segments; when the same action-shape appears
    both flagged-open and (elsewhere) is a known sensitive group, note it.
    """
    notes: List[str] = []
    by_tail: Dict[str, List[Finding]] = {}
    for f in findings:
        tail = "/".join(f.path.rstrip("/").split("/")[-2:])
        by_tail.setdefault(tail, []).append(f)
    for tail, group in by_tail.items():
        paths = {g.path for g in group}
        if len(paths) > 1:
            notes.append(
                f"duplicate action '{tail}' exposed at multiple paths: {sorted(paths)}"
            )
    return notes


# ---------------------------------------------------------------------------
# Network probing (stdlib only)
# ---------------------------------------------------------------------------


@dataclass
class Finding(Serializable):
    method: str
    path: str
    status_no_token: int
    status_with_token: Optional[int]
    severity: str
    reason: str


@dataclass
class SweepResult(Serializable):
    base_url: str
    swept: int
    include_mutating: bool = False
    skipped_mutating: int = 0  # mutating endpoints not probed (safe default)
    flagged: List[Finding] = field(default_factory=list)
    duplicate_notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _status(url: str, method: str, token: Optional[str], timeout: float) -> int:
    """Return the HTTP status for one probe (0 on transport error)."""
    req = urllib.request.Request(url=url, method=method.upper())
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if method.upper() in _MUTATING:
        req.add_header("Content-Type", "application/json")
        req.data = b"{}"
    try:
        with urllib.request.urlopen(
            req, timeout=timeout
        ) as resp:  # noqa: S310  # nosec B310 - probing user-specified web target
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 — transport failure → sentinel 0
        return 0


def probe(
    base_url: str,
    method: str,
    path: str,
    token: Optional[str],
    timeout: float = 30.0,
) -> Tuple[int, Optional[int]]:
    """Probe one endpoint with no token and (if given) with a token."""
    url = base_url.rstrip("/") + substitute_params(path)
    no_token = _status(url, method, None, timeout)
    with_token = _status(url, method, token, timeout) if token else None
    return no_token, with_token


def load_openapi(source: str, base_url: str, timeout: float = 30.0) -> Dict[str, Any]:
    """Load an OpenAPI spec from a file path or URL (defaults to <base>/openapi.json)."""
    if source and re.match(r"^https?://", source):
        with urllib.request.urlopen(
            source, timeout=timeout
        ) as r:  # noqa: S310  # nosec B310 - openapi source restricted to http(s) above
            return json.loads(r.read().decode("utf-8"))
    if source:
        with open(source, encoding="utf-8") as fh:
            return json.load(fh)
    return load_openapi(base_url.rstrip("/") + "/openapi.json", base_url, timeout)


def sweep(
    base_url: str,
    openapi: Dict[str, Any],
    token: Optional[str] = None,
    include_mutating: bool = False,
    max_workers: int = 12,
    timeout: float = 30.0,
) -> SweepResult:
    """Probe enumerated endpoints with/without a token and flag exposures.

    Safe by default: mutating verbs are skipped unless ``include_mutating`` is
    set (see :func:`filter_probeable`). Each worker returns ``(finding, error)``
    so nothing is appended to shared lists across threads.
    """
    endpoints, skipped = filter_probeable(
        enumerate_endpoints(openapi), include_mutating
    )
    findings: List[Finding] = []
    errors: List[str] = []

    def _run(pair: Tuple[str, str]) -> Tuple[Optional[Finding], Optional[str]]:
        method, path = pair
        try:
            no_tok, with_tok = probe(base_url, method, path, token, timeout)
        except Exception as e:  # noqa: BLE001 — one bad probe must not sink the sweep
            return None, f"{method} {path}: {e}"
        verdict = classify(method, path, no_tok, with_tok)
        if verdict:
            return (
                Finding(
                    method=method,
                    path=path,
                    status_no_token=no_tok,
                    status_with_token=with_tok,
                    severity=verdict["severity"],
                    reason=verdict["reason"],
                ),
                None,
            )
        return None, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for finding, err in pool.map(_run, endpoints):
            if finding is not None:
                findings.append(finding)
            if err is not None:
                errors.append(err)

    order = {"critical": 0, "high": 1, "medium": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.path))
    return SweepResult(
        base_url=base_url,
        swept=len(endpoints),
        include_mutating=include_mutating,
        skipped_mutating=skipped,
        flagged=findings,
        duplicate_notes=find_duplicate_exposures(findings),
        errors=errors,
    )

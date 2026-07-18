"""Security-sweep pure logic — enumeration + classification (no network)."""

from engine.security import (
    Finding,
    classify,
    enumerate_endpoints,
    filter_probeable,
    find_duplicate_exposures,
    is_public,
    substitute_params,
)

_OPENAPI = {
    "paths": {
        "/api/auth/login": {"post": {}},
        "/api/clients/": {"get": {}, "post": {}},
        "/api/clients/{client_id}": {"get": {}, "delete": {}},
        "/api/health": {"get": {}},
        "/api/health/cache/clear": {"post": {}},
        "/api/health/profiling": {"get": {}},
        "/": {"get": {}},
    }
}


def test_enumerate_endpoints_flattens_and_sorts():
    eps = enumerate_endpoints(_OPENAPI)
    assert ("POST", "/api/auth/login") in eps
    assert ("GET", "/api/clients/") in eps
    assert ("DELETE", "/api/clients/{client_id}") in eps
    # sorted + de-duped
    assert eps == sorted(set(eps))


def test_substitute_params():
    assert substitute_params("/api/clients/{client_id}") == "/api/clients/test-id-0"
    assert substitute_params("/api/x/{a}/y/{b}") == "/api/x/test-id-0/y/test-id-0"
    assert substitute_params("/api/plain") == "/api/plain"


def test_is_public():
    assert is_public("/api/auth/login") is True
    assert is_public("/api/health") is True
    assert is_public("/") is True
    assert is_public("/api/clients/") is False
    assert is_public("/api/health/cache/clear") is False  # not a basic probe
    # stateless public utilities / catalogs are allowed open (no false positives)
    assert is_public("/api/pricing/calculate") is True
    assert is_public("/api/credits/estimate") is True
    assert is_public("/api/generator/templates") is True
    # but protected sibling credit endpoints must NOT be treated as public
    assert is_public("/api/credits/balance") is False
    assert is_public("/api/credits/purchase") is False


def test_is_public_sensitive_fragment_override():
    # a private sibling under a public prefix must NOT be treated as public
    assert is_public("/api/pricing/history") is False
    assert is_public("/api/auth/me") is False
    assert is_public("/api/pricing/user/export") is False
    # genuinely-public catalog/config still public
    assert is_public("/api/pricing/config") is True
    assert is_public("/api/auth/login") is True


def test_filter_probeable_is_safe_by_default():
    eps = [
        ("GET", "/a"),
        ("POST", "/b"),
        ("DELETE", "/c/{id}"),
        ("GET", "/d"),
        ("PATCH", "/e"),
    ]
    keep, skipped = filter_probeable(eps, include_mutating=False)
    assert skipped == 3  # POST, DELETE, PATCH withheld
    assert set(keep) == {("GET", "/a"), ("GET", "/d")}
    keep_all, skipped_all = filter_probeable(eps, include_mutating=True)
    assert skipped_all == 0 and len(keep_all) == 5


def test_classify_public_and_rejected_are_not_findings():
    assert classify("POST", "/api/auth/login", 200, 200) is None  # public
    assert classify("GET", "/api/clients/", 401, 200) is None  # correctly rejected
    assert classify("DELETE", "/api/clients/{id}", 403, 204) is None
    assert classify("GET", "/api/clients/", 404, 200) is None  # 404 exposes nothing
    assert classify("POST", "/api/briefs/parse", 422, 422) is None  # ambiguous


def test_classify_flags_unauthenticated_exposure():
    crit = classify("POST", "/api/health/cache/clear", 200, 200)
    assert crit and crit["severity"] == "critical"  # mutating + sensitive + success

    high = classify("GET", "/api/health/profiling", 200, 200)
    assert high and high["severity"] == "high"  # sensitive info leak

    med = classify("GET", "/api/some/list", 200, 200)
    assert med and med["severity"] == "medium"  # plain data exposure


def test_classify_non_sensitive_post_is_not_critical():
    # a POST verb alone must NOT escalate to critical — many POSTs are stateless
    # calculators; only sensitive-group mutations are critical.
    v = classify("POST", "/api/widgets/", 200, 200)
    assert v and v["severity"] == "medium"


def test_classify_5xx_without_token_counts_as_executed():
    v = classify("POST", "/api/assistant/chat", 500, 500)
    assert v and "executed" in v["reason"]


def test_duplicate_exposure_notes():
    findings = [
        Finding("POST", "/api/health/cache/clear", 200, 200, "critical", "x"),
        Finding("POST", "/api/other/cache/clear", 200, 200, "critical", "x"),
    ]
    notes = find_duplicate_exposures(findings)
    assert notes and "cache/clear" in notes[0]

"""Serialization contract for the engine models."""

from engine.models import (
    Action,
    ActionType,
    ConsoleDelta,
    EvidenceBundle,
    GateCheckResult,
    GateResult,
    NetworkCall,
)


def test_to_dict_converts_enums_to_values():
    action = Action(
        type=ActionType.CLICK, selector="button.x", inferred_intent="submit"
    )
    d = action.to_dict()
    assert d["type"] == "click"  # enum -> value, not the Enum object
    assert d["selector"] == "button.x"
    assert d["inferred_intent"] == "submit"


def test_evidence_bundle_nested_serialization():
    bundle = EvidenceBundle(
        action=Action(type=ActionType.NAVIGATE, url="https://e.com"),
        url_before="https://e.com",
        url_after="https://e.com/next",
        http=[NetworkCall(method="GET", url="https://e.com/next", status=200)],
        console=ConsoleDelta(errors=["boom"], warnings=[]),
        gate=GateResult(
            passed=True, checks=[GateCheckResult(name="no_crash", passed=True)]
        ),
    )
    d = bundle.to_dict()
    assert d["action"]["type"] == "navigate"
    assert d["http"][0] == {"method": "GET", "url": "https://e.com/next", "status": 200}
    assert d["console"] == {"errors": ["boom"], "warnings": []}
    assert d["gate"]["passed"] is True
    assert d["gate"]["checks"][0]["name"] == "no_crash"
    assert d["screenshot"] is None

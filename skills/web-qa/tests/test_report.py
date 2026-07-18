"""ReportGenerator rendering — pure, works on dicts, no browser."""

import json

from engine.reporting import ReportGenerator


def _results():
    return {
        "metadata": {
            "run_id": "run-1",
            "target_url": "https://e.com",
            "engine": "chromium",
            "actions_total": 3,
            "actions_capped": True,
        },
        "evidence": [{"action": {"type": "click"}}],
        "issues": [
            {
                "id": "issue-001",
                "title": "Broken new-tab link",
                "severity": "high",
                "category": "navigation",
                "source": "deterministic",
                "url": "https://e.com",
                "description": "itch.io link 404s",
                "gate": {
                    "passed": False,
                    "checks": [
                        {
                            "name": "opened_pages_ok",
                            "passed": False,
                            "detail": "404 ...",
                        }
                    ],
                },
            },
            {
                "id": "issue-002",
                "title": "Silent no-op on primary CTA",
                "severity": "medium",
                "category": "functional",
                "source": "ai",
                "url": "https://e.com",
                "description": "clicking did nothing observable",
                "ai_verdict": {
                    "verdict": "inappropriate",
                    "confidence": "medium",
                    "reasoning": "no state change for an action that implies one",
                },
            },
        ],
    }


def test_render_writes_three_files(tmp_path):
    paths = ReportGenerator(output_dir=str(tmp_path)).render(_results())
    for key in ("json", "issues", "markdown"):
        assert paths[key].exists()


def test_issues_json_has_counts(tmp_path):
    ReportGenerator(output_dir=str(tmp_path)).render(_results())
    data = json.loads((tmp_path / "issues.json").read_text(encoding="utf-8"))
    assert data["total"] == 2
    assert data["high"] == 1
    assert data["medium"] == 1
    assert data["critical"] == 0


def test_markdown_shows_gate_and_ai_verdict(tmp_path):
    ReportGenerator(output_dir=str(tmp_path)).render(_results())
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "web-qa Report" in md
    assert "opened_pages_ok" in md  # deterministic gate failure surfaced
    assert "AI verdict:" in md  # advisory verdict surfaced
    assert "_(advisory)_" in md  # ai-sourced issue labeled advisory
    assert "(capped)" in md  # cap disclosure


def test_no_issues_says_so(tmp_path):
    clean = {"metadata": {"target_url": "https://e.com"}, "evidence": [], "issues": []}
    ReportGenerator(output_dir=str(tmp_path)).render(clean)
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "No issues found." in md

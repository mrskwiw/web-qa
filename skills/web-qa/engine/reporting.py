"""Report generation (Phase C/E).

Renders an assembled results object (a ``QAReport``-shaped dict the agent builds
from explore + act outputs and its judgments) into three files: ``report.json``
(full), ``issues.json`` (issues + severity counts), and ``report.md`` (human
readable, showing deterministic gate results alongside AI verdicts).

Works on plain dicts so the agent can hand back JSON without the engine needing
to reconstruct every dataclass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

_SEVERITIES = ("critical", "high", "medium", "low")


def _severity_counts(issues: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {s: 0 for s in _SEVERITIES}
    for issue in issues:
        sev = str(issue.get("severity", "")).lower()
        if sev in counts:
            counts[sev] += 1
    return counts


class ReportGenerator:
    def __init__(self, output_dir: str = "./qa-results") -> None:
        self.output_dir = Path(output_dir)

    def render(self, results: Dict[str, Any]) -> Dict[str, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        issues = results.get("issues", [])
        counts = _severity_counts(issues)

        report_json = self.output_dir / "report.json"
        report_json.write_text(json.dumps(results, indent=2), encoding="utf-8")

        issues_json = self.output_dir / "issues.json"
        issues_json.write_text(
            json.dumps({"total": len(issues), **counts, "issues": issues}, indent=2),
            encoding="utf-8",
        )

        report_md = self.output_dir / "report.md"
        report_md.write_text(self._markdown(results, counts), encoding="utf-8")

        return {"json": report_json, "issues": issues_json, "markdown": report_md}

    def _markdown(self, results: Dict[str, Any], counts: Dict[str, int]) -> str:
        meta = results.get("metadata", {})
        evidence = results.get("evidence", [])
        issues = results.get("issues", [])
        out: List[str] = []

        out.append("# web-qa Report\n")
        out.append(f"- **Target:** {meta.get('target_url', '?')}")
        out.append(
            f"- **Run:** {meta.get('run_id', '?')} · engine {meta.get('engine', '?')}"
        )
        out.append(
            f"- **Actions:** {meta.get('actions_total', len(evidence))}"
            f"{' (capped)' if meta.get('actions_capped') else ''}"
        )
        out.append(
            f"- **Issues:** {len(issues)}  "
            f"(critical {counts['critical']} · high {counts['high']} · "
            f"medium {counts['medium']} · low {counts['low']})\n"
        )

        if not issues:
            out.append("No issues found.\n")
        else:
            out.append("## Issues\n")
            for i in issues:
                out.append(self._issue_md(i))

        return "\n".join(out) + "\n"

    @staticmethod
    def _issue_md(issue: Dict[str, Any]) -> str:
        lines: List[str] = []
        sev = str(issue.get("severity", "?")).upper()
        src = str(issue.get("source", "?"))
        lines.append(f"### {issue.get('id', '?')} · {issue.get('title', '(untitled)')}")
        lines.append(
            f"**{sev}** · {issue.get('category', '?')} · source: `{src}`"
            f"{' _(advisory)_' if src == 'ai' else ''}"
        )
        if issue.get("url"):
            lines.append(f"URL: {issue['url']}")
        if issue.get("description"):
            lines.append(f"\n{issue['description']}")

        gate = issue.get("gate")
        if gate and not gate.get("passed", True):
            failed = [c for c in gate.get("checks", []) if not c.get("passed")]
            lines.append("\n**Gate failures:**")
            for c in failed:
                lines.append(f"- `{c.get('name')}` — {c.get('detail') or 'failed'}")

        verdict = issue.get("ai_verdict")
        if verdict:
            conf = verdict.get("confidence", "?")
            lines.append(
                f"\n**AI verdict:** {verdict.get('verdict', '?')} "
                f"(confidence: {conf}) — {verdict.get('reasoning', '')}"
            )
        if issue.get("screenshot"):
            lines.append(f"\n![evidence]({issue['screenshot']})")
        lines.append("")
        return "\n".join(lines)

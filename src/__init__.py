"""
QA Tool initialization
"""

__version__ = "0.1.0"
__author__ = "QA Tool Team"

from src.qa_standalone import (
    QATool,
    Action,
    Scenario,
    ActionType,
    OutcomeType,
    Severity,
    IssueCategory,
    BrowserState,
    ActionResult,
    Issue,
    QAReport,
)

__all__ = [
    "QATool",
    "Action",
    "Scenario",
    "ActionType",
    "OutcomeType",
    "Severity",
    "IssueCategory",
    "BrowserState",
    "ActionResult",
    "Issue",
    "QAReport",
]

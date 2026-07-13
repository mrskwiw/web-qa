"""web-qa engine — deterministic browser-automation + evidence capture.

The engine is the "hands" of the web-qa skill: it drives a headless browser,
captures structured evidence, and (Phase B) applies objective gate checks. It
contains no AI and makes no appropriateness judgments — that is the agent's job
(see ../SKILL.md and docs/QA_SKILL_SPECIFICATION.md).
"""

__version__ = "2.0.0-dev"

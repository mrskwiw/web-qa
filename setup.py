#!/usr/bin/env python3
"""Setup configuration for the web-qa engine package (v2)."""

from pathlib import Path

from setuptools import find_packages, setup

_here = Path(__file__).parent
_readme = _here / "README.md"
long_description = (
    _readme.read_text(encoding="utf-8")
    if _readme.exists()
    else "Deterministic engine for the web-qa Claude Code skill (explore/act/flow/sweep/report)."
)

with open(_here / "requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [
        line.strip() for line in fh if line.strip() and not line.startswith("#")
    ]

setup(
    name="web-qa-engine",
    version="2.2.0",
    author="QA Tool Team",
    description="Deterministic browser-driving engine for the web-qa Claude Code skill",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/qa-tool",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Testing",
    ],
    python_requires=">=3.9",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "web-qa=engine.cli:main",
        ],
    },
)

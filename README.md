# web-qa (Claude Code plugin)

AI-assisted web-application QA — a replacement for human QA, not a technical checker. Invoked
against a URL inside a Claude Code session, the agent enters the app as a real user with a real
goal, drives a headless browser to simulate end-to-end journeys, captures evidence (DOM, console,
network, screenshot), applies fast deterministic gates, then judges whether the goal was actually
achieved — catching bugs that pass every objective check but are still wrong (silent no-ops,
wrong/empty content, misleading state, broken links, billed actions that produce nothing).

## Layout

```
project/                       ← plugin root
├── .claude-plugin/
│   └── plugin.json            ← plugin manifest
└── skills/
    └── web-qa/                ← the skill Claude Code loads
        ├── SKILL.md           ← agent instructions (the "AI" half)
        ├── engine/            ← deterministic Python engine (explore/act/flow/a11y/sweep/report)
        ├── requirements.txt
        └── tests/
```

The **agent** (`SKILL.md`) does all the AI: infers expected behavior from a page snapshot and
judges evidence against that inferred intent. The **engine** (`engine/`) is a deterministic
instrument the agent shells out to via CLI subcommands — it contains no AI and makes no
appropriateness judgments. The two halves communicate through a stable evidence-bundle JSON
contract.

## Install

```
/plugin marketplace add mrskwiw/mrskwiw-plugins
/plugin install web-qa@mrskwiw-plugins
```

This directory is also a self-contained Claude Code plugin, so a local plugin install can point at
it directly. Either way, set up the engine's Python dependencies once:

```bash
cd skills/web-qa
pip install -r requirements.txt
python -m playwright install chromium
```

## Engine commands

Invoked as a subprocess from `SKILL.md`, from the skill directory (`skills/web-qa/`). The
engine uses package-relative imports, so run it as a module (`python -m engine.cli …`), not as a
loose script:

```bash
python -m engine.cli explore --url <URL>                       # ranked page snapshot (+ accessibility report)
python -m engine.cli act --url <URL> --action <json>           # execute one action → evidence bundle + gate
python -m engine.cli flow --url <URL> --steps <json>           # drive an end-to-end journey (stateful)
python -m engine.cli a11y --url <URL>                          # WCAG A/AA accessibility audit
python -m engine.cli sweep --url <BASE> --token-env <VAR>      # endpoint auth-enforcement sweep
python -m engine.cli report --input results.json --output ./qa-results  # render results
```

`sweep` is the one HTTP-level check (everything else drives the real UI). It is **safe by default**
— read-only `GET` probes only; `--include-mutating` sends real writes that would execute on an
unprotected endpoint, so use it only against a test target you own.

## Tests

```bash
cd skills/web-qa
pytest
```

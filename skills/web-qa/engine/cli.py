"""Engine CLI — the agent's hands (``explore`` / ``act`` / ``report``).

The engine is stateless per action: ``explore`` inventories a page, ``act`` runs
one interaction and returns a gated evidence bundle, ``report`` renders assembled
results. Orchestration and cost controls (``--max-actions``, ``--flow``) live in
the agent workflow (``SKILL.md``), not here — the engine has no notion of a run.

Invoke as a module from the skill dir::

    python -m engine.cli explore --url https://example.com
    python -m engine.cli act --url https://example.com --action '{"type":"click","selector":"a"}'
    python -m engine.cli report --input results.json --output ./qa-results
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict

import click

from . import security
from .browser import BrowserController
from .evidence import EvidenceBundler
from .flow import build_action, evaluate_assertion, fail_reason, slug
from .gate import DeterministicGate
from .models import Action, ActionType, BrowserEngine
from .reporting import ReportGenerator

_ENGINE_CHOICE = click.Choice([e.value for e in BrowserEngine])


def _emit(payload: Dict[str, Any], output: str | None) -> None:
    """Print JSON to stdout, and also write it to ``output`` when given."""
    text = json.dumps(payload, indent=2)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
    click.echo(text)


@click.group()
def cli() -> None:
    """web-qa deterministic engine."""


@cli.command()
@click.option("--url", required=True, help="Page to snapshot.")
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--output", type=click.Path(), default=None, help="Also write snapshot JSON here."
)
def explore(url: str, engine: str, headless: bool, output: str | None) -> None:
    """Navigate to URL and emit a structured, ranked page snapshot as JSON."""

    async def run():
        controller = BrowserController(engine=BrowserEngine(engine), headless=headless)
        await controller.launch()
        try:
            await controller.navigate(url)
            return await controller.capture_snapshot()
        finally:
            await controller.close()

    snapshot = asyncio.run(run())
    _emit(snapshot.to_dict(), output)


@cli.command()
@click.option("--url", required=True, help="Page to load before acting.")
@click.option(
    "--action",
    "action_json",
    required=True,
    help='Action JSON, e.g. \'{"type":"click","selector":"a.cta","inferred_intent":"open signup"}\'',
)
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--screenshot", type=click.Path(), default=None, help="Optional screenshot path."
)
@click.option(
    "--output", type=click.Path(), default=None, help="Also write bundle JSON here."
)
def act(
    url: str,
    action_json: str,
    engine: str,
    headless: bool,
    screenshot: str | None,
    output: str | None,
) -> None:
    """Execute one action and emit a gated evidence bundle."""
    data = json.loads(action_json)
    action = Action(
        type=ActionType(data["type"]),
        selector=data.get("selector"),
        url=data.get("url"),
        text=data.get("text"),
        key=data.get("key"),
        value=data.get("value"),
        inferred_intent=data.get("inferred_intent"),
    )

    async def run():
        controller = BrowserController(engine=BrowserEngine(engine), headless=headless)
        await controller.launch()
        try:
            await controller.navigate(url)
            before = await controller.capture_state()
            await controller.perform(action)
            await controller.settle_popups()
            after = await controller.capture_state()
            shot = await controller.screenshot(screenshot) if screenshot else None
            target_present = (
                await controller.is_present(action.selector)
                if action.selector
                else None
            )
            return EvidenceBundler().build(
                action,
                before,
                after,
                screenshot=shot,
                target_present=target_present,
                opened=controller.opened_pages(),
            )
        finally:
            await controller.close()

    bundle = asyncio.run(run())
    bundle.gate = DeterministicGate().evaluate(bundle)
    _emit(bundle.to_dict(), output)


@cli.command()
@click.option("--url", required=True, help="Entry URL loaded once before step 1.")
@click.option(
    "--steps",
    "steps_path",
    required=True,
    type=click.Path(exists=True),
    help="Steps JSON: a list, or {'steps': [...]}. Each step is an action + optional 'assert'.",
)
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--continue-on-fail",
    is_flag=True,
    default=False,
    help="Keep running later steps after a step fails (default: halt at the failed step).",
)
@click.option(
    "--screenshot-dir",
    type=click.Path(),
    default=None,
    help="If set, capture a screenshot after every step into this dir.",
)
@click.option(
    "--output", type=click.Path(), default=None, help="Also write flow JSON here."
)
def flow(
    url: str,
    steps_path: str,
    engine: str,
    headless: bool,
    continue_on_fail: bool,
    screenshot_dir: str | None,
    output: str | None,
) -> None:
    """Run an ordered list of steps in ONE browser context (stateful, spec §4.2).

    One evidence bundle + gate + assertion per step. By default the flow HALTS at
    the first failed step (gate fail, assertion fail, or the action raising) so
    later steps never run on top of an unmet precondition; ``--continue-on-fail``
    overrides. Secrets are referenced by env var (``{"env":"VAR"}`` or ``${VAR}``)
    and never inlined in the steps file.
    """
    raw = json.loads(Path(steps_path).read_text(encoding="utf-8"))
    steps = raw["steps"] if isinstance(raw, dict) else raw
    env = os.environ

    async def run():
        controller = BrowserController(engine=BrowserEngine(engine), headless=headless)
        await controller.launch()
        results = []
        halted_at = None
        try:
            await controller.navigate(url)
            for i, step in enumerate(steps):
                label = step.get("label") or f"step-{i + 1}"
                action = build_action(step, env)
                before = await controller.capture_state()
                opened_mark = len(controller.opened_pages())
                perform_error = None
                try:
                    await controller.perform(action)
                    await controller.settle_popups()
                    aw = step.get("await_response")
                    if aw:
                        await controller.wait_for_api(
                            aw["path_contains"],
                            method=aw.get("method"),
                            since=len(before.network),
                            timeout_ms=int(aw.get("timeout_ms", 15000)),
                        )
                    if step.get("settle_ms"):
                        await controller.settle(int(step["settle_ms"]))
                except Exception as exc:  # noqa: BLE001 — a step error halts the flow
                    perform_error = str(exc)
                after = await controller.capture_state()
                opened = controller.opened_pages()[opened_mark:]
                shot = None
                if screenshot_dir:
                    shot = await controller.screenshot(
                        str(Path(screenshot_dir) / f"{i + 1:02d}-{slug(label)}.png")
                    )
                target_present = (
                    await controller.is_present(action.selector)
                    if action.selector
                    else None
                )
                bundle = EvidenceBundler().build(
                    action,
                    before,
                    after,
                    screenshot=shot,
                    target_present=target_present,
                    opened=opened,
                )
                bundle.gate = DeterministicGate().evaluate(bundle)
                assertion = evaluate_assertion(bundle, step.get("assert"))
                passed = (
                    bundle.gate.passed and assertion.passed and perform_error is None
                )
                results.append(
                    {
                        "label": label,
                        "bundle": bundle.to_dict(),
                        "assertion": assertion.to_dict(),
                        "perform_error": perform_error,
                        "passed": passed,
                    }
                )
                if not passed and not continue_on_fail:
                    halted_at = {
                        "index": i,
                        "label": label,
                        "reason": fail_reason(bundle.gate, assertion, perform_error),
                    }
                    break
        finally:
            await controller.close()
        return {
            "metadata": {
                "target_url": url,
                "engine": engine,
                "steps_total": len(steps),
                "steps_run": len(results),
                "halted": halted_at is not None,
            },
            "steps": results,
            "halted_at": halted_at,
        }

    _emit(asyncio.run(run()), output)


@cli.command()
@click.option("--url", required=True, help="Base URL of the target API/app.")
@click.option(
    "--openapi",
    default="",
    help="OpenAPI spec path or URL (default: <url>/openapi.json).",
)
@click.option(
    "--token-env",
    default=None,
    help="Env var holding a bearer token for the authenticated baseline probe.",
)
@click.option(
    "--include-mutating",
    is_flag=True,
    default=False,
    help="Also probe POST/PUT/PATCH/DELETE. These send REAL requests that WOULD "
    "execute on an unprotected endpoint (delete/clear/etc) — use only on test "
    "targets. Default: skip them (safe) and report how many were skipped.",
)
@click.option(
    "--output", type=click.Path(), default=None, help="Also write sweep JSON here."
)
def sweep(
    url: str,
    openapi: str,
    token_env: str | None,
    include_mutating: bool,
    output: str | None,
) -> None:
    """Auth-enforcement sweep: probe endpoints with/without a token (spec §6.1).

    Safe by default — read-only (GET) probes; pass --include-mutating on a test
    target to also probe write verbs (which can execute on exposed endpoints).
    """
    token = os.environ.get(token_env) if token_env else None
    spec = security.load_openapi(openapi, url)
    result = security.sweep(url, spec, token=token, include_mutating=include_mutating)
    _emit(result.to_dict(), output)


@cli.command()
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True),
    help="Assembled results JSON (metadata + evidence + issues).",
)
@click.option(
    "--output",
    type=click.Path(),
    default="./qa-results",
    help="Report output directory.",
)
def report(input_path: str, output: str) -> None:
    """Render report.md / report.json / issues.json from assembled results."""
    results = json.loads(Path(input_path).read_text(encoding="utf-8"))
    paths = ReportGenerator(output_dir=output).render(results)
    click.echo(json.dumps({k: str(v) for k, v in paths.items()}, indent=2))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

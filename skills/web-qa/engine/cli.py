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
import sys
import time
from pathlib import Path
from typing import Any, Dict

import click

from . import security
from .browser import BrowserController
from .evidence import EvidenceBundler
from .flow import build_action, evaluate_assertion, fail_reason, slug
from .gate import DeterministicGate
from .interact import InteractError
from .interact import click as interact_click_impl
from .interact import fill as interact_fill_impl
from .interact import read as interact_read_impl
from .interact import start_session as start_interact_session
from .interact import stop as interact_stop_impl
from .models import Action, ActionType, BrowserEngine
from .recorder import RECORDER_JS, Recording, RecordedEvent, RecordedRoute, summarize
from .reporting import ReportGenerator

_ENGINE_CHOICE = click.Choice([e.value for e in BrowserEngine])

# BUGS.md 2026-08-21: `content_after` can run to 20,000 chars (~10k tokens) and
# appears once per step, so a 15-action flow's page text alone can approach the
# agent's whole context budget for no benefit once the full text is safely on
# disk. Echoing a bounded excerpt instead -- only once --output is given, so
# nothing is ever silently lost relative to today's behavior -- lets an agent
# judge outcomes from the excerpt and read the file deliberately when it needs
# more, instead of paying for the whole 20k on every single action.
_STDOUT_EXCERPT_CHARS = 500


def _truncate_for_stdout(value: Any, output: str) -> Any:
    """Recursively copy ``value``, shortening any ``content_after`` string for
    the STDOUT echo only. The full value is what gets written to ``output``;
    this never touches the payload that ``_emit`` passes to ``write_text``.
    """
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for k, v in value.items():
            if (
                k == "content_after"
                and isinstance(v, str)
                and len(v) > _STDOUT_EXCERPT_CHARS
            ):
                result[k] = (
                    v[:_STDOUT_EXCERPT_CHARS]
                    + f"\n\n[stdout truncated: showing {_STDOUT_EXCERPT_CHARS} of "
                    f"{len(v)} chars -- full content in {output}]"
                )
            else:
                result[k] = _truncate_for_stdout(v, output)
        return result
    if isinstance(value, list):
        return [_truncate_for_stdout(v, output) for v in value]
    return value


def _emit(payload: Dict[str, Any], output: str | None) -> None:
    """Print JSON to stdout, and also write it to ``output`` when given.

    Omitting ``output`` keeps today's behaviour exactly as-is (full content on
    stdout) -- only PASSING ``--output`` can shrink what's echoed, and even
    then the file on disk always carries the untruncated payload.
    """
    text = json.dumps(payload, indent=2)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
        click.echo(json.dumps(_truncate_for_stdout(payload, output), indent=2))
    else:
        click.echo(text)


def _load_session(session: str | None) -> tuple[Any, str | None]:
    """Load a saved auth session bundle → (storage_state, user_agent).

    Returns ``(None, None)`` when no session path is given. The bundle is what
    ``flow --save-session`` writes: ``{"user_agent": ..., "storage_state": {...}}``.
    Reusing it seeds a context with the saved cookies so runs are authenticated
    WITHOUT re-logging-in (avoiding auth rate limits + bot challenges), and pins
    the user-agent the token's device fingerprint was bound to.
    """
    if not session:
        return None, None
    data = json.loads(Path(session).read_text(encoding="utf-8"))
    return data.get("storage_state"), data.get("user_agent")


def _controller(
    engine: str,
    headless: bool,
    session: str | None = None,
    user_agent: str | None = None,
    low_memory: bool = False,
) -> BrowserController:
    """Build a BrowserController, seeding a saved auth session when provided.

    An explicit ``--user-agent`` overrides the one recorded in the session bundle
    (use the same UA you logged in with, or leave it to inherit from the bundle).
    """
    storage_state, session_ua = _load_session(session)
    return BrowserController(
        engine=BrowserEngine(engine),
        headless=headless,
        storage_state=storage_state,
        user_agent=user_agent or session_ua,
        low_memory=low_memory,
    )


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
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Reuse a saved auth session (from `flow --save-session`) so this run is logged in.",
)
@click.option(
    "--user-agent",
    default=None,
    help="Override the user-agent (defaults to the one saved in --session).",
)
@click.option(
    "--output", type=click.Path(), default=None, help="Also write snapshot JSON here."
)
@click.option(
    "--low-memory/--no-low-memory",
    default=False,
    help="Launch Chromium with conservative memory-reduction flags (weaker "
    "baseline resource use; trades nothing functional). Worth it when several "
    "of these run concurrently (fan-out) or the host is otherwise memory-tight.",
)
def explore(
    url: str,
    engine: str,
    headless: bool,
    session: str | None,
    user_agent: str | None,
    output: str | None,
    low_memory: bool,
) -> None:
    """Navigate to URL and emit a structured, ranked page snapshot as JSON."""

    async def run():
        controller = _controller(engine, headless, session, user_agent, low_memory)
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
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Reuse a saved auth session (from `flow --save-session`) so this run is logged in.",
)
@click.option(
    "--user-agent",
    default=None,
    help="Override the user-agent (defaults to the one saved in --session).",
)
@click.option(
    "--output", type=click.Path(), default=None, help="Also write bundle JSON here."
)
@click.option(
    "--low-memory/--no-low-memory",
    default=False,
    help="Launch Chromium with conservative memory-reduction flags (weaker "
    "baseline resource use; trades nothing functional). Worth it when several "
    "of these run concurrently (fan-out) or the host is otherwise memory-tight.",
)
def act(
    url: str,
    action_json: str,
    engine: str,
    headless: bool,
    screenshot: str | None,
    session: str | None,
    user_agent: str | None,
    output: str | None,
    low_memory: bool,
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
        controller = _controller(engine, headless, session, user_agent, low_memory)
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
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Reuse a saved auth session so the flow starts already logged in.",
)
@click.option(
    "--save-session",
    type=click.Path(),
    default=None,
    help="After the flow completes, save the context's auth session (cookies + UA) here "
    "for reuse via --session. Use this to run a login flow ONCE and replay it.",
)
@click.option(
    "--user-agent",
    default=None,
    help="User-agent for the run. Pin the SAME UA for login (--save-session) and every "
    "replay (--session), since auth tokens are bound to a UA+IP fingerprint.",
)
@click.option(
    "--destructive/--no-destructive",
    default=False,
    help="Declare this flow destructive. Refuses to run unless --yes is also passed "
    "-- running a destructive flow means really performing it (a delete, a "
    "cancellation), so it requires explicit confirmation on top of the session's "
    "own Bash permission prompt, which a human can miss when it's buried in step "
    "6 of a raw steps.json.",
)
@click.option(
    "--costs/--no-costs",
    default=False,
    help="Declare this flow costed -- spends real credits or money even though it "
    "is not destructive (a paid research run, a billed API call). Same gate as "
    "--destructive: refuses to run unless --yes is also passed.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Confirm running a --destructive and/or --costs flow.",
)
@click.option(
    "--output", type=click.Path(), default=None, help="Also write flow JSON here."
)
@click.option(
    "--low-memory/--no-low-memory",
    default=False,
    help="Launch Chromium with conservative memory-reduction flags (weaker "
    "baseline resource use; trades nothing functional). Worth it when several "
    "of these run concurrently (fan-out) or the host is otherwise memory-tight.",
)
def flow(
    url: str,
    steps_path: str,
    engine: str,
    headless: bool,
    continue_on_fail: bool,
    screenshot_dir: str | None,
    session: str | None,
    save_session: str | None,
    user_agent: str | None,
    destructive: bool,
    costs: bool,
    yes: bool,
    output: str | None,
    low_memory: bool,
) -> None:
    """Run an ordered list of steps in ONE browser context (stateful, spec §4.2).

    One evidence bundle + gate + assertion per step. By default the flow HALTS at
    the first failed step (gate fail, assertion fail, or the action raising) so
    later steps never run on top of an unmet precondition; ``--continue-on-fail``
    overrides. Secrets are referenced by env var (``{"env":"VAR"}`` or ``${VAR}``)
    and never inlined in the steps file.

    A flow declared ``--destructive`` and/or ``--costs`` refuses to run (nothing
    launched, ``metadata.refused: true``) unless ``--yes`` is also passed -- the
    session's own Bash permission prompt is the outer backstop, but a human
    skimming a raw steps.json for a real delete or paid call buried mid-script can
    miss it; this makes the agent state the risk itself, in the command it runs.
    """
    raw = json.loads(Path(steps_path).read_text(encoding="utf-8"))
    steps = raw["steps"] if isinstance(raw, dict) else raw
    env = os.environ

    if (destructive or costs) and not yes:
        reasons = []
        if destructive:
            reasons.append("destructive")
        if costs:
            reasons.append("costed")
        refused: Dict[str, Any] = {
            "metadata": {
                "target_url": url,
                "engine": engine,
                "steps_total": len(steps),
                "steps_run": 0,
                "halted": True,
                "session_saved": None,
                "refused": True,
                "reason": f"refused: {'/'.join(reasons)} flow requires --yes",
            },
            "steps": [],
            "halted_at": None,
        }
        _emit(refused, output)
        return

    async def run():
        controller = _controller(engine, headless, session, user_agent, low_memory)
        await controller.launch()
        results = []
        halted_at = None
        session_saved = None
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
            # Persist the auth session (cookies + UA) while the context is still open,
            # so a login flow can be run once and replayed via --session. Best-effort:
            # a save failure must not sink the flow's results.
            if save_session:
                try:
                    session_saved = await controller.save_session(
                        save_session, user_agent=user_agent
                    )
                except Exception as exc:  # noqa: BLE001
                    session_saved = f"ERROR: {exc}"
        finally:
            await controller.close()
        return {
            "metadata": {
                "target_url": url,
                "engine": engine,
                "steps_total": len(steps),
                "steps_run": len(results),
                "halted": halted_at is not None,
                "session_saved": session_saved,
                "refused": False,
            },
            "steps": results,
            "halted_at": halted_at,
        }

    _emit(asyncio.run(run()), output)


@cli.command()
@click.option("--url", required=True, help="Where the session starts.")
@click.option(
    "--output", type=click.Path(), required=True, help="Write the recording JSON here."
)
@click.option(
    "--screenshot-dir",
    type=click.Path(),
    default=None,
    help="Capture a screenshot of every screen recorded.",
)
@click.option(
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Start already authenticated from a saved session.",
)
@click.option(
    "--save-session",
    type=click.Path(),
    default=None,
    help="Save the session on exit — so the login you just did by hand can be "
    "replayed headlessly by every later run.",
)
@click.option(
    "--max-minutes", default=30, help="Hard stop, so a forgotten window can't run forever."
)
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--user-agent", default=None, help="Pin the UA (see --save-session).")
@click.option(
    "--low-memory/--no-low-memory",
    default=False,
    help="Launch Chromium with conservative memory-reduction flags (weaker "
    "baseline resource use; trades nothing functional).",
)
def record(
    url: str,
    output: str,
    screenshot_dir: str | None,
    session: str | None,
    save_session: str | None,
    max_minutes: int,
    engine: str,
    user_agent: str | None,
    low_memory: bool,
) -> None:
    """Watch a HUMAN use the app, and capture every screen they reach.

    The inverse of ``flow``: you navigate, the engine records. Use it for the
    surfaces automated discovery cannot reach — anything behind a login, a bot
    challenge, a paywall, or (as on React Native Web apps) a control the ranker
    cannot distinguish from ninety others.

    Drive the app normally. Every distinct URL is inventoried automatically.
    **Press Ctrl+Shift+S to capture the current screen on demand** — that is the
    only way to record a modal, a drawer, a wizard step or a creator panel, none
    of which change the URL. Close the browser window when you are done.

    Passwords and other credential-shaped values are redacted in the page, before
    they ever reach Python, so recording a real login is safe.
    """
    shots = Path(screenshot_dir) if screenshot_dir else None

    async def run():
        controller = _controller(engine, False, session, user_agent, low_memory)
        await controller.launch()

        events: list[RecordedEvent] = []
        routes: list[RecordedRoute] = []
        seen_urls: set[str] = set()
        manual_requests: list[dict] = []

        def on_event(raw: dict) -> None:
            if raw.get("type") == "snapshot_request":
                manual_requests.append(raw)
                return
            events.append(
                RecordedEvent(
                    type=str(raw.get("type")),
                    t=int(raw.get("t") or 0),
                    url=str(raw.get("url") or ""),
                    element=raw.get("element"),
                    value=raw.get("value"),
                    key=raw.get("key"),
                    via=raw.get("via"),
                    fields=raw.get("fields"),
                )
            )

        await controller.install_recorder(RECORDER_JS, on_event)
        controller.wire_page(controller.page)
        controller.context.on("page", controller.wire_page)

        async def capture(trigger: str) -> None:
            page = controller.page
            try:
                snapshot = await controller.capture_snapshot()
                title = await page.title()
            except Exception as exc:  # noqa: BLE001 — a screen we cannot read is
                # still worth listing; dropping it would silently shrink the map.
                snapshot, title = None, f"<capture failed: {exc}>"
            shot = None
            if shots:
                name = f"{len(routes) + 1:03d}-{slug(page.url.split('/')[-1] or 'screen')}.png"
                try:
                    shot = await controller.screenshot(str(shots / name))
                except Exception:  # noqa: BLE001
                    shot = None
            routes.append(
                RecordedRoute(
                    url=page.url,
                    title=title,
                    trigger=trigger,
                    at=int(time.time() * 1000),
                    screenshot=shot,
                    snapshot=snapshot,
                )
            )
            click.echo(
                f"  [{len(routes):3d}] {trigger:6s} {page.url}"
                f"{'' if snapshot is None else f' ({len(snapshot.interactive)} controls)'}",
                err=True,
            )

        click.echo(f"Recording. Browser open at {url}", err=True)
        click.echo(
            "  Drive the app normally — log in, open the creators, walk the settings.\n"
            "  Ctrl+Shift+S  capture the current screen (modals, drawers, wizard steps)\n"
            "  Close the window when you're done.",
            err=True,
        )

        await controller.navigate(url)
        deadline = time.time() + max_minutes * 60
        last_url = None
        stable_since = 0.0

        while time.time() < deadline:
            live = controller.live_pages()
            if not live:
                break
            # Follow the human between tabs: the newest live page is the one
            # they are looking at.
            if controller.page.is_closed() or controller.page not in live:
                controller.set_active_page(live[-1])

            while manual_requests:
                manual_requests.pop(0)
                await capture("manual")

            try:
                current = controller.page.url
            except Exception:  # noqa: BLE001 — mid-navigation
                await asyncio.sleep(0.4)
                continue

            if current != last_url:
                last_url = current
                stable_since = time.time()
            elif (
                stable_since
                and time.time() - stable_since > 1.2
                and current not in seen_urls
                and not current.startswith("about:")
            ):
                # Snapshot only once the URL has held still, so a redirect chain
                # records its destination rather than each hop.
                seen_urls.add(current)
                await capture("url")

            await asyncio.sleep(0.4)

        saved = None
        if save_session:
            try:
                saved = await controller.save_session(save_session, user_agent=user_agent)
            except Exception as exc:  # noqa: BLE001
                saved = f"ERROR: {exc}"

        recording = Recording(
            metadata={
                "start_url": url,
                "engine": engine,
                "stopped": "window closed" if time.time() < deadline else "max-minutes",
                "session_saved": saved,
            },
            routes=routes,
            events=events,
            network=controller.recorded_network(),
            console_errors=controller.console_errors(),
        )
        try:
            await controller.close()
        except Exception:  # noqa: BLE001 — the human already closed it
            pass
        return recording

    recording = asyncio.run(run())
    payload = recording.to_dict()
    payload["summary"] = summarize(recording)
    _emit(payload, output)


@cli.command()
@click.option("--url", required=True, help="Page to audit for accessibility.")
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Reuse a saved auth session so an authenticated route can be audited.",
)
@click.option(
    "--user-agent",
    default=None,
    help="Override the user-agent (defaults to the one saved in --session).",
)
@click.option(
    "--output", type=click.Path(), default=None, help="Also write a11y JSON here."
)
@click.option(
    "--low-memory/--no-low-memory",
    default=False,
    help="Launch Chromium with conservative memory-reduction flags (weaker "
    "baseline resource use; trades nothing functional). Worth it when several "
    "of these run concurrently (fan-out) or the host is otherwise memory-tight.",
)
def a11y(
    url: str,
    engine: str,
    headless: bool,
    session: str | None,
    user_agent: str | None,
    output: str | None,
    low_memory: bool,
) -> None:
    """Deterministic accessibility (WCAG A/AA) audit of one page (spec §3d).

    Emits objective violations only (missing alt/label/lang, empty title, bad
    heading order, positive tabindex, duplicate id) — the keyboard-nav and
    screen-reader *coherence* judgment is the agent's, per SKILL.md §3d. The same
    report is also embedded in every ``explore`` snapshot under ``accessibility``.
    """

    async def run():
        controller = _controller(engine, headless, session, user_agent, low_memory)
        await controller.launch()
        try:
            await controller.navigate(url)
            return await controller.capture_a11y()
        finally:
            await controller.close()

    report = asyncio.run(run())
    _emit(report.to_dict(), output)


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
    "targets. Default: skip them (safe) and report how many were skipped. "
    "Refuses to run unless --yes is also passed.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Confirm running a --include-mutating sweep.",
)
@click.option(
    "--output", type=click.Path(), default=None, help="Also write sweep JSON here."
)
def sweep(
    url: str,
    openapi: str,
    token_env: str | None,
    include_mutating: bool,
    yes: bool,
    output: str | None,
) -> None:
    """Auth-enforcement sweep: probe endpoints with/without a token (spec §6.1).

    Safe by default — read-only (GET) probes; pass --include-mutating on a test
    target to also probe write verbs (which can execute on exposed endpoints).
    A mutating sweep refuses to run (nothing probed, ``refused: true``) unless
    ``--yes`` is also passed — same self-declared-risk gate as ``flow
    --destructive``/``--costs``: the session's own Bash permission prompt is the
    outer backstop, but --include-mutating fires real writes at every endpoint
    the spec advertises, and that risk deserves its own explicit confirmation
    rather than riding in on however permissively the session happens to be
    configured (BUGS.md 2026-08-14).
    """
    if include_mutating and not yes:
        _emit(
            {
                "base_url": url,
                "swept": 0,
                "include_mutating": True,
                "skipped_mutating": 0,
                "coverage": "REFUSED — mutating sweep requires --yes",
                "flagged": [],
                "duplicate_notes": [],
                "errors": [],
                "refused": True,
                "reason": "refused: mutating sweep requires --yes",
            },
            output,
        )
        return
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


@cli.group()
def interact() -> None:
    """A persistent, agent-driven browser session spanning SEPARATE CLI calls.

    ``flow --steps`` needs a complete step sequence guessed upfront from static
    markup, bet on all at once against a real page -- workable for a short flow,
    unreliable for a gated multi-step journey (a signup wizard, a checkout) where
    a wrong guess three steps in gives no signal about which step was wrong.
    `start` once, then `click`/`fill`/`read` one action at a time against the SAME
    live page across as many separate invocations as it takes, `stop` when done --
    then write the now-empirically-known sequence as `flow --steps` and get the
    real evidence bundle + gate + assertion `flow` provides. No engine allowlist,
    no auto-chaining, no guessed values -- every action is one explicit call the
    agent chooses to make. See engine/interact.py.
    """


@interact.command("start")
@click.option("--url", required=True, help="Page to open once the session starts.")
@click.option(
    "--state",
    "state_path",
    required=True,
    type=click.Path(),
    help="Where to write this session's handle -- pass the SAME path to every "
    "later `interact` call. Refuses to overwrite an existing one (stop it first) "
    "so a browser process is never silently leaked.",
)
@click.option(
    "--session",
    default=None,
    type=click.Path(exists=True),
    help="Seed cookies/localStorage from a saved auth bundle (same format as "
    "`flow --session`).",
)
@click.option("--user-agent", default=None, help="Pin the user-agent for this session.")
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--timeout-s", default=10.0, type=float, help="How long to wait for chromium to start."
)
@click.option(
    "--low-memory/--no-low-memory",
    default=False,
    help="Launch Chromium with conservative memory-reduction flags. Worth it "
    "here especially: this process is DETACHED and can outlive the agent turn "
    "that started it if `stop` is forgotten (see `interact` --help).",
)
@click.option(
    "--chrome-path",
    default=None,
    help="Launch this browser BINARY (real Chrome/Edge/Brave/a channel build) "
    "instead of Playwright's bundled Chromium -- so the fingerprint is a real "
    "browser's. For a HUMAN-driven session past a wall that flags automation "
    "Chromium: you drive and clear the wall, this only observes. Not evasion "
    "(no webdriver masking / synthetic input) -- it IS the real browser.",
)
@click.option(
    "--real-chrome",
    is_flag=True,
    default=False,
    help="Convenience for --chrome-path: auto-locate the installed Google Chrome.",
)
@click.option(
    "--user-data-dir",
    "user_data_dir",
    default=None,
    type=click.Path(),
    help="Persistent profile dir (SURVIVES `stop`, unlike the default throwaway "
    "temp profile) -- log in / clear a challenge once by hand, and every later "
    "session reuses it. Use a DEDICATED dir, never your everyday Chrome's own "
    "default profile (Chrome refuses remote debugging on that, and it would be "
    "locked by any running Chrome).",
)
def interact_start(
    url: str,
    state_path: str,
    session: str | None,
    user_agent: str | None,
    headless: bool,
    timeout_s: float,
    low_memory: bool,
    chrome_path: str | None,
    real_chrome: bool,
    user_data_dir: str | None,
) -> None:
    """Launch a detached browser and navigate to --url."""
    try:
        result = start_interact_session(
            state_path, url, session=session, user_agent=user_agent,
            headless=headless, timeout_s=timeout_s, low_memory=low_memory,
            chrome_path=chrome_path, real_chrome=real_chrome,
            user_data_dir=user_data_dir,
        )
    except InteractError as exc:
        click.echo(json.dumps({"error": str(exc)}))
        sys.exit(1)
    _emit(result, None)


@interact.command("click")
@click.option("--state", "state_path", required=True, type=click.Path(exists=True))
@click.option("--text", default=None, help="Click the first element containing this text.")
@click.option(
    "--selector", default=None, help="Click by CSS/role selector instead of --text."
)
def interact_click(state_path: str, text: str | None, selector: str | None) -> None:
    """Click one control and report whether the page navigated or just changed."""
    try:
        result = interact_click_impl(state_path, text=text, selector=selector)
    except InteractError as exc:
        click.echo(json.dumps({"error": str(exc)}))
        sys.exit(1)
    _emit(result, None)


@interact.command("fill")
@click.option("--state", "state_path", required=True, type=click.Path(exists=True))
@click.option("--selector", required=True, help="Field to fill.")
@click.option("--value", required=True, help="Text to type.")
def interact_fill(state_path: str, selector: str, value: str) -> None:
    """Fill one field."""
    try:
        result = interact_fill_impl(state_path, selector, value)
    except InteractError as exc:
        click.echo(json.dumps({"error": str(exc)}))
        sys.exit(1)
    _emit(result, None)


@interact.command("read")
@click.option("--state", "state_path", required=True, type=click.Path(exists=True))
def interact_read(state_path: str) -> None:
    """Report the current URL/title/visible-text preview, no action taken."""
    try:
        result = interact_read_impl(state_path)
    except InteractError as exc:
        click.echo(json.dumps({"error": str(exc)}))
        sys.exit(1)
    _emit(result, None)


@interact.command("stop")
@click.option("--state", "state_path", required=True, type=click.Path(exists=True))
def interact_stop(state_path: str) -> None:
    """Kill the detached chromium and remove the session handle."""
    try:
        result = interact_stop_impl(state_path)
    except InteractError as exc:
        click.echo(json.dumps({"error": str(exc)}))
        sys.exit(1)
    _emit(result, None)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

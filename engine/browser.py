"""Headless-browser driver + evidence capture (Phase A).

``BrowserController`` wraps Playwright's async API. Its job is narrow: launch a
browser, perform one requested :class:`~engine.models.Action`, and capture a full
:class:`~engine.models.PageState`. It makes no judgments — deterministic gating
(Phase B) and semantic judgment (the agent) live elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from playwright.async_api import async_playwright

from .models import (
    Action,
    ActionType,
    BrowserEngine,
    ConsoleDelta,
    ConsoleMessage,
    FormField,
    FormInfo,
    IncompleteFeature,
    InteractiveElement,
    LinkInfo,
    NetworkCall,
    PageSnapshot,
    PageState,
)

# JS that emits a compact role/text tree of interactive + landmark elements.
# Structure over raw HTML keeps the outline token-efficient for the agent.
_DOM_OUTLINE_JS = r"""
() => {
  const MAX_LINES = 250;
  const lines = [];
  const INTERACTIVE = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA']);
  const LANDMARK = new Set(['NAV', 'MAIN', 'HEADER', 'FOOTER', 'FORM', 'SECTION', 'ASIDE']);

  const label = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim().slice(0, 80);
    if (el.tagName === 'INPUT') {
      return (el.getAttribute('placeholder') || el.getAttribute('name') || el.type || '').trim().slice(0, 80);
    }
    const t = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    return t.slice(0, 80);
  };

  const selectorFor = (el) => {
    if (el.id) return '#' + el.id;
    const testid = el.getAttribute('data-testid');
    if (testid) return '[data-testid="' + testid + '"]';
    const name = el.getAttribute('name');
    if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
    let cls = '';
    if (el.className && typeof el.className === 'string') {
      const parts = el.className.trim().split(/\s+/).slice(0, 2);
      if (parts[0]) cls = '.' + parts.join('.');
    }
    return el.tagName.toLowerCase() + cls;
  };

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };

  const walk = (el, depth) => {
    if (lines.length >= MAX_LINES) return;
    for (const child of el.children) {
      const tag = child.tagName;
      const isInt = INTERACTIVE.has(tag) || child.hasAttribute('role') || child.hasAttribute('onclick');
      const isLand = LANDMARK.has(tag);
      if ((isInt || isLand) && visible(child)) {
        const role = child.getAttribute('role') || tag.toLowerCase();
        const indent = '  '.repeat(Math.min(depth, 8));
        lines.push(indent + role + ' "' + label(child) + '" {' + selectorFor(child) + '}');
      }
      walk(child, depth + ((isInt || isLand) ? 1 : 0));
      if (lines.length >= MAX_LINES) return;
    }
  };

  if (document.body) walk(document.body, 0);
  return lines.join('\n');
}
"""

_FOCUS_JS = (
    "() => { const a = document.activeElement; "
    "if (!a || a === document.body) return null; "
    "return a.id ? '#' + a.id : a.tagName.toLowerCase(); }"
)

# Readable rendered text (what the *user* sees), for outcome verification — the
# agent judges the produced content against intent (e.g. did research actually
# yield keywords?). Distinct from the structural DOM outline; capped for tokens.
_CONTENT_JS = r"""
() => {
  const t = (document.body && document.body.innerText) ? document.body.innerText : '';
  return t.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim().slice(0, 4000);
}
"""

# Structured page inventory for the agent: ranked interactive elements, forms
# (with a destructive heuristic), and links (external / new-tab / scheme flags).
_SNAPSHOT_JS = r"""
() => {
  const originHost = location.host;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = (el) => {
    const a = el.getAttribute('aria-label');
    if (a) return a.trim().slice(0, 100);
    if (el.tagName === 'INPUT') {
      return (el.getAttribute('placeholder') || el.getAttribute('name') || el.type || '').trim().slice(0, 100);
    }
    return (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100);
  };
  const sel = (el) => {
    if (el.id) return '#' + el.id;
    const ti = el.getAttribute('data-testid');
    if (ti) return '[data-testid="' + ti + '"]';
    const nm = el.getAttribute('name');
    if (nm) return el.tagName.toLowerCase() + '[name="' + nm + '"]';
    let cls = '';
    if (el.className && typeof el.className === 'string') {
      const p = el.className.trim().split(/\s+/).slice(0, 2);
      if (p[0]) cls = '.' + p.join('.');
    }
    return el.tagName.toLowerCase() + cls;
  };
  const landmark = (el) => {
    let n = el.parentElement;
    while (n) {
      const t = n.tagName;
      if (t === 'MAIN' || t === 'HEADER' || t === 'FOOTER' || t === 'NAV' || t === 'FORM') return t;
      n = n.parentElement;
    }
    return 'BODY';
  };

  const interactive = [];
  const links = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('a, button, input, select, textarea, [role=button], [onclick]')) {
    if (!visible(el)) continue;
    const tag = el.tagName;
    const s = sel(el);
    const text = label(el);
    const role = el.getAttribute('role') || (tag === 'A' ? 'link' : tag === 'BUTTON' ? 'button' : tag.toLowerCase());
    const loc = landmark(el);
    let rank = 4, kind = 'other';
    if (loc === 'MAIN') { const big = (tag === 'BUTTON' || tag === 'A'); rank = big ? 0 : 2; kind = big ? 'cta' : 'field'; }
    else if (loc === 'HEADER' || loc === 'NAV') { rank = 1; kind = 'nav'; }
    else if (loc === 'FORM') { rank = 2; kind = 'field'; }
    else if (loc === 'FOOTER') { rank = 3; kind = 'footer'; }
    const key = role + '|' + text + '|' + s;
    if (!seen.has(key)) { seen.add(key); interactive.push({ selector: s, role, text, rank, kind }); }
    if (tag === 'A') {
      const rawHref = el.getAttribute('href');
      const href = rawHref || '';
      let scheme = 'relative', external = false;
      try {
        const u = new URL(href, location.href);
        scheme = u.protocol.replace(':', '');
        external = (u.host !== originHost) && (scheme === 'http' || scheme === 'https');
      } catch (e) { scheme = href ? 'unknown' : 'relative'; }
      // A link that goes nowhere: no href, '#', or a javascript: sink.
      const dead = (rawHref === null) || href === '' || href === '#'
        || /^javascript:/i.test(href);
      links.push({ selector: s, text, href, target: el.getAttribute('target'), external, scheme, dead });
    }
  }

  // Incomplete / not-built markers a human tester would notice. Extracted from
  // the page's visible text in ONE pass — a single body.innerText (which already
  // covers ALL visible elements incl. div/p, so nothing is missed by markup) then
  // a cheap line scan. No per-element walk: fast on huge DOMs AND complete.
  const INCOMPLETE_RE = /(coming soon|under construction|not implemented|work in progress|placeholder|lorem ipsum|to be added|\btbd\b|\bwip\b)/i;
  const incomplete = [];
  const bodyText = document.body ? document.body.innerText : '';
  if (INCOMPLETE_RE.test(bodyText)) {
    const lines = bodyText.split('\n').map((s) => s.trim()).filter(Boolean);
    const seenInc = new Set();
    for (let i = 0; i < lines.length && incomplete.length < 30; i++) {
      const m = lines[i].match(INCOMPLETE_RE);
      if (!m) continue;
      // Label = the marker line; if that's just the bare marker, prepend the
      // previous line (usually the feature name, e.g. "Voice Analysis").
      let label = lines[i];
      if (label.length < 25 && i > 0 && !INCOMPLETE_RE.test(lines[i - 1])) {
        label = lines[i - 1] + ' — ' + label;
      }
      label = label.slice(0, 90);
      if (!seenInc.has(label)) { seenInc.add(label); incomplete.push({ marker: m[0].toLowerCase(), label }); }
    }
  }

  const forms = [];
  for (const f of document.querySelectorAll('form')) {
    if (!visible(f)) continue;
    const fields = [];
    for (const inp of f.querySelectorAll('input, select, textarea')) {
      fields.push({
        selector: sel(inp),
        name: inp.getAttribute('name'),
        type: (inp.getAttribute('type') || inp.tagName.toLowerCase()),
        label: label(inp),
      });
    }
    // Resolve the true submit control in priority order. A bare
    // `querySelector('..., button')` returns the FIRST button in DOM order,
    // which on many forms is an in-field control (e.g. a show-password toggle),
    // not the submit. Prefer explicit submit, then a button that is not
    // explicitly non-submit, taking the LAST such (submit is usually last).
    const nonSubmit = [...f.querySelectorAll('button:not([type=button]):not([type=reset])')];
    const submitEl =
      f.querySelector('button[type=submit]') ||
      f.querySelector('input[type=submit]') ||
      f.querySelector('input[type=image]') ||
      (nonSubmit.length ? nonSubmit[nonSubmit.length - 1] : null) ||
      f.querySelector('button');
    const text = (f.innerText || '').toLowerCase();
    // A password field alone is NOT destructive — a login is idempotent auth,
    // not a record-creating action. Flag on destructive wording, or on a
    // password field that is NOT on a login form (likely registration / set-password).
    const DESTRUCTIVE = /\b(pay|checkout|purchase|place order|delete|remove|cancel account|unsubscribe|sign\s*up|register|create account)\b/;
    const LOGIN = /\b(log\s*in|sign\s*in)\b/;
    const hasPassword = fields.some((x) => x.type === 'password');
    const destructive = DESTRUCTIVE.test(text) || (hasPassword && !LOGIN.test(text));
    forms.push({ selector: sel(f), fields, submit: submitEl ? sel(submitEl) : null, destructive });
  }

  interactive.sort((a, b) => a.rank - b.rank);
  return { interactive, forms, links, incomplete };
}
"""


class BrowserController:
    """Drive a single page and capture its observable state."""

    def __init__(
        self,
        engine: BrowserEngine = BrowserEngine.CHROMIUM,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        timeout_ms: int = 15000,
        slowmo_ms: int = 0,
    ) -> None:
        self._engine = engine
        self._headless = headless
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._timeout = timeout_ms
        self._slowmo = slowmo_ms

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

        # Cumulative capture (deltas derived by the EvidenceBundler).
        self._console: List[ConsoleMessage] = []
        self._network: List[NetworkCall] = []
        self._page_errors: List[str] = []

        # New tabs / popups opened by an action (e.g. target="_blank" links).
        self._popup_pages: list = []
        self._popups: List[NetworkCall] = []

    # -- lifecycle ---------------------------------------------------------

    async def launch(self) -> None:
        self._pw = await async_playwright().start()
        browser_type = getattr(self._pw, self._engine.value)
        self._browser = await browser_type.launch(
            headless=self._headless, slow_mo=self._slowmo
        )
        self._context = await self._browser.new_context(viewport=self._viewport)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self._timeout)
        self._wire_listeners()

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    def _wire_listeners(self) -> None:
        self._page.on(
            "console",
            lambda msg: self._console.append(
                ConsoleMessage(level=msg.type, text=msg.text)
            ),
        )
        self._page.on("pageerror", lambda exc: self._page_errors.append(str(exc)))
        self._page.on("response", self._on_response)
        # Attached after the main page exists, so it only fires for popups.
        self._context.on("page", self._on_popup)

    def _on_response(self, response) -> None:
        try:
            self._network.append(
                NetworkCall(
                    method=response.request.method,
                    url=response.url,
                    status=response.status,
                )
            )
        except (
            Exception
        ):  # noqa: BLE001  # nosec B110 - a bad record must not abort the run
            pass

    def _on_popup(self, page) -> None:
        """Track a new tab/popup opened by an action; status is resolved in settle_popups."""
        self._popup_pages.append(page)

    # -- actions -----------------------------------------------------------

    async def navigate(self, url: str) -> None:
        await self._page.goto(url, wait_until="networkidle", timeout=self._timeout)

    async def perform(self, action: Action) -> None:
        """Dispatch a single action, then wait briefly for the page to settle."""
        t = action.type
        if t is ActionType.NAVIGATE:
            await self.navigate(_require(action.url, "url"))
        elif t is ActionType.CLICK:
            await self._page.click(_require(action.selector, "selector"))
        elif t is ActionType.FILL:
            await self._page.fill(
                _require(action.selector, "selector"), action.value or ""
            )
        elif t is ActionType.TYPE:
            await self._page.type(
                _require(action.selector, "selector"), action.text or ""
            )
        elif t is ActionType.PRESS:
            await self._page.press(
                action.selector or "body", _require(action.key, "key")
            )
        elif t is ActionType.SCROLL:
            distance = int(action.value) if action.value else 500
            await self._page.mouse.wheel(0, distance)
        elif t is ActionType.WAIT_FOR:
            await self._page.wait_for_selector(_require(action.selector, "selector"))
        else:  # pragma: no cover — enum is exhaustive
            raise ValueError(f"Unsupported action type: {t}")
        await self._page.wait_for_timeout(300)

    async def settle(self, ms: int) -> None:
        """Extra idle wait after an action — for slow SPA transitions/XHR to land
        before the after-state is captured (per-step ``settle_ms`` in a flow)."""
        await self._page.wait_for_timeout(ms)

    def network_len(self) -> int:
        """Count of network calls captured so far (a mark for per-step deltas)."""
        return len(self._network)

    async def wait_for_api(
        self,
        path_contains: str,
        method: Optional[str] = None,
        since: int = 0,
        timeout_ms: int = 15000,
        poll_ms: int = 250,
    ) -> bool:
        """Wait until a network response matching ``path_contains`` (+ optional
        ``method``) appears after index ``since``, or ``timeout_ms`` elapses.

        For long-running actions (LLM/research calls) whose response lands after
        a fixed settle window — poll the captured network so the after-state's
        http delta actually includes the awaited response. Returns whether it
        arrived.
        """
        want = method.upper() if method else None
        waited = 0
        while True:
            for c in self._network[since:]:
                if path_contains in c.url and (
                    want is None or c.method.upper() == want
                ):
                    return True
            if waited >= timeout_ms:
                return False
            await self._page.wait_for_timeout(poll_ms)
            waited += poll_ms

    async def settle_popups(self, timeout_ms: int = 5000) -> None:
        """Load any new tabs opened by the last action and record their document status.

        A popup's original navigation response fires before a listener can attach
        (Playwright race), so we resolve the *settled* URL's status with a follow-up
        context request. Reliable for catching broken new-tab / ``target="_blank"``
        links, which the on-page checks otherwise miss entirely.
        """
        for page in self._popup_pages:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except (
                Exception
            ):  # noqa: BLE001  # nosec B110 - a popup that never settles is not fatal
                pass
            url = page.url
            if not url.startswith(("http://", "https://")):
                continue  # about:blank, mailto:, javascript: — nothing to fetch
            try:
                resp = await self._context.request.get(url, timeout=timeout_ms)
                self._popups.append(
                    NetworkCall(method="GET", url=resp.url, status=resp.status)
                )
            except (
                Exception
            ):  # noqa: BLE001 — a failed fetch is recorded as unreachable
                self._popups.append(NetworkCall(method="GET", url=url, status=0))
        self._popup_pages = []

    def opened_pages(self) -> List[NetworkCall]:
        """Document status for tabs/popups opened by an action (resolved in settle_popups)."""
        return list(self._popups)

    async def is_present(self, selector: str) -> bool:
        """Whether a selector currently resolves to an element in the DOM."""
        return await self._page.query_selector(selector) is not None

    async def screenshot(self, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=str(target))
        return str(target)

    # -- capture -----------------------------------------------------------

    def _console_delta(self) -> ConsoleDelta:
        return ConsoleDelta(
            errors=[m.text for m in self._console if m.level == "error"],
            warnings=[m.text for m in self._console if m.level in ("warning", "warn")],
        )

    async def capture_snapshot(self) -> PageSnapshot:
        """Structured, ranked page inventory for the agent's intent inference."""
        raw = await self._page.evaluate(_SNAPSHOT_JS)
        interactive = [
            InteractiveElement(
                selector=e["selector"],
                role=e["role"],
                text=e["text"],
                kind=e["kind"],
                rank=e["rank"],
            )
            for e in raw["interactive"]
        ]
        forms = [
            FormInfo(
                selector=f["selector"],
                fields=[
                    FormField(
                        selector=x["selector"],
                        type=x["type"],
                        name=x["name"],
                        label=x["label"],
                    )
                    for x in f["fields"]
                ],
                submit=f["submit"],
                destructive=f["destructive"],
            )
            for f in raw["forms"]
        ]
        links = [
            LinkInfo(
                selector=lk["selector"],
                text=lk["text"],
                href=lk["href"],
                scheme=lk["scheme"],
                external=lk["external"],
                new_tab=(lk["target"] == "_blank"),
                dead=lk.get("dead", False),
            )
            for lk in raw["links"]
        ]
        incomplete = [
            IncompleteFeature(marker=m["marker"], label=m["label"])
            for m in raw.get("incomplete", [])
        ]
        return PageSnapshot(
            url=self._page.url,
            title=await self._page.title(),
            interactive=interactive,
            forms=forms,
            links=links,
            incomplete=incomplete,
            console=self._console_delta(),
        )

    async def capture_state(self) -> PageState:
        cookies = await self._context.cookies()
        cookie_map = {c["name"]: str(c.get("value", "")) for c in cookies}
        return PageState(
            url=self._page.url,
            title=await self._page.title(),
            ready_state=await self._page.evaluate("document.readyState"),
            console=list(self._console),
            network=list(self._network),
            page_errors=list(self._page_errors),
            focus=await self._page.evaluate(_FOCUS_JS),
            cookies=cookie_map,
            dom_outline=await self._page.evaluate(_DOM_OUTLINE_JS),
            content=await self._page.evaluate(_CONTENT_JS),
        )


def _require(value: Optional[str], field_name: str) -> str:
    if not value:
        raise ValueError(f"Action is missing required field: {field_name!r}")
    return value

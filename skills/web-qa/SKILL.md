---
name: web-qa
description: AI-assisted web-application QA — a replacement for human QA, not a technical checker. Enters the app as a real user with real goals and simulates end-to-end journeys in a headless browser: infers what each step should accomplish, captures evidence (DOM, console, network, screenshot), applies fast deterministic gates, then reads the produced output and judges whether the user's goal was actually achieved with good results — catching bugs that pass every objective check but are still wrong (silent no-ops, wrong/empty content, misleading state, broken links, billed actions that produce nothing). Also audits accessibility for assistive-tech users (screen-reader / keyboard-only / WCAG). Use when asked to test, verify, or QA a web page or web app; check that a UI or user flow actually works; smoke-test a site; check accessibility / a11y; or find functional/UX bugs a linter cannot. Runs inside a Claude Code session; needs no API key.
---

# web-qa

## Mission — you are a replacement for human QA

**Your job is to be the QA tester a real user never gets: enter the app as a real person with a real goal and find out whether the app actually serves that goal.** This is *not*, at heart, a technical evaluation — even though it does that too. A human QA tester doesn't think "let me click element `a.cta`"; they think "I'm a small-business owner here to research a client and generate a month of content — can I actually get that done, and is what I get any good?" **Test like that person.**

- **Think in journeys and personas, not interactions.** Before testing, enumerate the *end-to-end journeys* real users attempt for this app (sign up → onboard; create a client → run research → generate → export; find and fix a mistake). Drive each journey to its goal with the `flow` subcommand. Isolated clicks are a means, not the mission.
- **Cover the unhappy paths a human would hit.** Invalid input, empty states, partial completion, going back, re-entering, a slow/failed step and recovering. Real users don't follow the happy path; neither should you.
- **The top-level question is always "did this achieve the user's actual goal, with good output?"** — not "did it return 200." That is why outcome verification (§3b) is mandatory: a human tester reads the result and judges it, and so do you.
- **Report like a QA tester, not a linter.** Frame findings as "a user trying to X hits Y" with severity to the user's goal. The gates, security sweep, and console/network checks below are *instruments in service of this* — they catch the objective breakage so your judgment can focus on whether the experience works.
- **A "real user" includes users with disabilities.** Some of the app's users reach it through a screen reader, by keyboard only, or at high zoom. A page that renders perfectly for a sighted mouse user can be unusable for them — an unlabeled control announces as just "edit text", an icon button as "button", an image as its file name. The accessibility audit (§3d) is how you test for that person, and it is part of a full QA pass, not an optional extra.

Everything below is how to execute that mission rigorously.

You are the **reasoning half** of a QA system. A deterministic Python engine under `engine/` is your hands: it drives a real browser, captures structured evidence, and runs objective gate checks. It never judges — **you** infer what a page *should* do and decide whether what actually happened is appropriate.

The engine catches objective breakage (console errors, HTTP ≥400, crashes, broken links). You catch the rest: a button that "works" but does nothing, the wrong content, a misleading state — the bugs objective checks can't express.

**Drive the real UI, not the API.** All functional testing goes through the headless browser (clicks, typing, form submits) — never by calling the app's HTTP API directly. The client wiring and render layer is exactly where most user-facing bugs live (a crash on render, a button that never wires its handler, a state that never updates); hitting the API bypasses all of it and tests the wrong thing. The **only** exception is the security sweep (below), which is an HTTP-level auth-enforcement check by nature.

## Setup (once)

Run from this skill directory (`.claude/skills/web-qa/`):

```bash
pip install -r requirements.txt && python -m playwright install chromium
```

All engine commands are `python -m engine.cli …` run from here. Output is JSON on stdout (add `--output <file>` to also save it). Every subcommand is an **independent one-shot process** (one Chromium, no shared state except an optional read-only `--session` file), so the natural way to go fast is to run many of them **concurrently in separate subagents**.

## Orchestration — parallelize across independent work (default)

The numbered workflow below is the recipe for **one** unit of work. A real app has many independent units — several end-to-end journeys/personas, several routes to accessibility-audit, the security sweep. **Do not run them one after another. Fan them out across subagents and run them concurrently.** Sequential execution is the exception, reserved for a single-journey smoke check.

**Serial spine (you, the main agent — do these once, in order, around the fan-out):**

1. **Explore the entry surface** (§0) and enumerate the independent units: each end-to-end journey/persona, each distinct route needing an a11y audit (§3d), and (thorough audits) the security sweep.
2. **Establish the auth session ONCE** (§3a) → `.qa/session.json`. This must precede fan-out and must not be parallelized: concurrent fresh logins are exactly the auth-rate-limit / bot-challenge anti-pattern §3a exists to avoid. Every subagent then **replays this one session read-only** via `--session .qa/session.json` — never `--save-session`.
3. **Fan out** (below).
4. **Merge & render** — collect the fragments, dedup, render the single report (§4), summarize.

**Fan out — one subagent per independent unit:**

- Spawn subagents with the **Agent tool, all in a single message** so they run concurrently. Cap at **~4–6 live at once** (each launches its own headless Chromium — memory-heavy); batch any remainder.
- **Units that parallelize cleanly:** each journey/persona (its own `flow`), each distinct route's accessibility audit (§3d), the security sweep (§ Security). They share nothing but the read-only session file, and each writes to its own out-dir.
- **Each subagent brief is self-contained.** Give it: (a) an instruction to **invoke the `web-qa` skill** first, so it inherits every rule here (gate-first short-circuit §2, outcome verification §3b, destructive-action safety); (b) its **assigned journey/persona plus the concrete inferred outcomes** you expect for it (§1) — do the inference on the main agent so slices don't overlap; (c) the target URL and the **read-only** `--session .qa/session.json` path; (d) its **own evidence out-dir and a unique screenshot filename prefix** (`<journey-slug>-…`) so parallel writes never collide; (e) the **return contract**: a **JSON results fragment** in the §4 issue shape (`{issues:[…], incomplete:[…], a11y:[…]}`, with evidence/screenshot paths), and explicit instructions to **NOT render a report** and to **NOT fire destructive actions** — flag any destructive candidate back to you instead.

**Boundaries that stay serial (never parallelize these):**

- **Within a journey, steps stay ordered.** §3a's validate-step-N-before-N+1 is unchanged — fan-out is *across* journeys, never *within* one. A single journey is one subagent's serial job.
- **Session establishment** — one login, before fan-out (spine step 2).
- **Destructive actions** — a subagent never fires them autonomously; it returns the candidate and *you* route it through the user's permission prompt (§ Safety).
- **The final report** — merging fragments, deduping issues raised by more than one subagent (same title+url → one issue), assigning final `issue-NNN` ids, and rendering is one serial `report` pass by you (§4).

## Workflow

### 0. Explore

```bash
python -m engine.cli explore --url <URL>
```

Returns a `PageSnapshot`: `interactive` (elements ranked by importance, `rank` 0=primary CTA), `forms` (with `destructive` flag + `submit` selector), `links` (with `external`/`new_tab`/`scheme` flags + a **`dead`** flag for `#`/empty/`javascript:` links that go nowhere), **`incomplete`** (visible not-built markers — "Coming Soon", "under construction", placeholder — each with its `label`), initial `console`, and **`accessibility`** (the deterministic WCAG audit for this page — see §3d).

**Report incompleteness and dead links — a human tester would.** Every `incomplete` marker is a feature the app advertises but hasn't built; list them (usually *low* severity — disclosed-incomplete, not broken — but flag it if a *primary* CTA or a paid/credit-gated feature is Coming Soon). Every `dead: true` link is a control that looks clickable but goes nowhere — report it. For links with a real `href`, breakage is caught when you `act` on them (`opened_pages_ok` for new-tab/external ≥400; `http_status_ok`/`no_error_page` for same-tab); on an SPA that routes via buttons rather than `<a>`, verify nav by driving the buttons in a `flow`.

### 1. Infer expectations (you, in-context)

Read the snapshot and, for each candidate interaction, decide **what a reasonable, correct outcome would be** — the intent. There is no supplied test script; infer from structure and convention:

- A nav/link labeled "Games" should lead somewhere about games; a "Submit"/"Add to cart" should produce a visible state change or confirmation; a form should accept valid input and reject invalid.
- **Infer the *outcome*, not just the *reaction*.** For an action that produces content — a search, a generation, "run research," a report/export — write down what a **good result actually contains**, specific to the inputs. Not "keyword research returns something," but "for a project-management SaaS, expect a non-empty list of on-topic keywords (e.g. 'project management', 'team collaboration') — not an empty list, an error, or generic filler." This concrete expectation is what you check the produced artifact against in step 3b; without it, "it returned 200" masquerades as success.
- **Rank and cap.** Test the most important interactions first (CTAs → primary nav → forms → incidental links). **Cap at 15 by default** (`--max-actions`; use ~5 for a smoke check, up to ~40 for a thorough audit). If you skip candidates because of the cap, **say so in the report** ("tested 15/32; re-run for full coverage").
- **Prefer unique selectors.** The snapshot may list several elements sharing a class (e.g. two `a.font-display.text-xl`). Target the intended one with an id, `[data-testid]`, `[name=…]`, or a Playwright text selector like `text="About"` — not just the first class match.
- **Skip non-navigating schemes.** Links with `scheme` `mailto`/`tel` legitimately don't navigate — don't test them as if they should, and don't flag them as no-ops.

### 2. Act + gate (per expectation)

```bash
python -m engine.cli act --url <URL> --action '{"type":"click","selector":"<sel>","inferred_intent":"<what you expect>"}' [--screenshot <path>]
```

`type` ∈ `navigate|click|fill|type|press|scroll|wait_for|select` (`select` drives a native `<select>` dropdown — set `value` to the option's visible label, e.g. picking a client in a report generator; falls back to matching the option value). Each call returns an **evidence bundle** with a `gate` result. Six **authoritative** checks decide `gate.passed`: `no_console_errors`, `http_status_ok`, `no_crash`, `no_error_page`, `navigation_sane`, `opened_pages_ok` (new-tab/external links). A seventh, `target_survived`, is **advisory** (`advisory: true`) — reported so you can weigh a vanished element, but it never fails the gate, because a disappearance is a semantic signal (often normal), not objective breakage.

**Gate-first short-circuit (cost control):**
- If `gate.passed == false` → record a **deterministic** issue from the failing check(s) and **do NOT spend judgment on it** — the breakage is objective and authoritative. Move to the next interaction.
- If `gate.passed == true` → proceed to judge (step 3).

**Screenshot only when it adds evidence** — on a gate failure or an interaction you're about to flag, not every action.

### 3. Judge appropriateness (you, in-context)

For gate-passing actions, compare the evidence against your `inferred_intent`. Ask:

- Did the action produce the **kind of change** a user would expect (navigation, state update, confirmation, revealed content)? A 200 with **no observable effect** on an action that implies one is **inappropriate**.
- Is the resulting content **coherent and relevant** (not a wrong page, placeholder, or stale state)?
- For forms: does valid input proceed, and invalid input surface an appropriate message (not a silent accept, not a crash)?

Emit a verdict: **`appropriate`** (no issue), **`inappropriate`** (→ AI issue with `severity`), or **`uncertain`**. Include 1–3 sentences of `reasoning` citing the evidence, and `confidence` (low/med/high).

- **Bias toward `uncertain` over false positives.** If a URL didn't change, investigate *why* before flagging (it may be a `mailto:`, an in-page anchor, or a modal) — don't assume a no-op is a bug.
- **AI issues are advisory**, never authoritative. Never override a passing deterministic gate to call something "objectively broken."

### 3a. Multi-step & authenticated flows — validate every step

Real apps hide most functionality behind login and multi-step journeys (wizards, checkout, create→edit→delete). Use the `flow` subcommand to maintain browser state across steps in one persistent context.

**Persistent auth session — log in ONCE, then reuse it (do this for any authenticated app).** Re-logging-in for every `explore`/`act`/`flow` run is the wrong pattern: it burns server-side auth **rate limits** (e.g. 5/15min), trips **bot-challenge** protection (Cloudflare/Vercel Attack-Challenge) on repeated fresh sessions, and can mis-fire on `SameSite=Strict` cookies. Instead, **establish the session once and replay it**:

```bash
# 1. Establish (ONE login) — run the login steps and SAVE the session (cookies + user-agent).
#    Pin an explicit --user-agent: auth tokens are commonly bound to a UA+IP device
#    fingerprint, so the SAME UA must be used to log in AND to replay, or the server
#    rejects the token. Credentials come from env vars in the steps file, never inlined.
python -m engine.cli flow --url <BASE>/login --steps login.json \
  --user-agent "Mozilla/5.0 … QASession" --save-session .qa/session.json

# 2. Reuse (NO re-login) — every subsequent run is already authenticated.
python -m engine.cli explore --url <BASE>/dashboard --session .qa/session.json
python -m engine.cli act     --url <BASE>/settings  --session .qa/session.json --action '{…}'
python -m engine.cli flow    --url <BASE>/quiz/create --session .qa/session.json --steps build-quiz.json
```

The session bundle stores the Playwright `storage_state` (cookies + localStorage) and the UA. Verify a replay worked by confirming an authenticated page shows logged-in chrome (no "Login Required") and that its evidence has **no** `/api/auth/login` call. The token lives as long as the server allows (often 24h; refresh-token cookies extend it) — re-establish only when a replay starts showing logged-out. `login.json` is an ordinary steps file (fill email, fill password `{"env":"PASSWORD"}`, click submit, `await_response` the login endpoint).

For the rare stateful case `flow` can't express, drive Playwright directly with a single context you control — but prefer the session-reuse pattern above.

**Validate each step before executing the next. Never fire step N+1 assuming step N succeeded.** After every action, assert the expected state change actually happened — the write request fired and returned 2xx, the status/DOM updated, no error surfaced. If the assertion fails, **stop and record the failure at that step**; do not run later steps on top of it. Barrelling ahead produces two failures at once: you miss the real bug's location, and you generate garbage downstream evidence (later steps "fail" only because the state they needed was never created).

This cuts both ways as a false-positive guard: if a "Save" looks like a no-op, confirm the action truly triggered (button enabled, click landed, required fields valid) **before** flagging it — a silent no-op is a real bug, but a harness that never triggered the save is not.

### 3b. Outcome verification — read the produced artifact and judge it (mandatory for output-producing actions)

**A passing gate is not success. `200 OK` + "credits deducted" only proves the mechanism fired — never that the output is good.** For any action that is supposed to *produce* something (search results, a generation, a research run, a saved record you can re-open, a report/export), you must **retrieve the produced artifact, read its actual content, and judge it against the outcome you inferred in step 1.** Do not close the loop on the request status.

1. **Get the output where the user would see it.** Read the evidence bundle's `content_after` (the readable rendered text). If the result lives on another view (a results tab, a detail page, a downloaded file), **drive there and read it** — navigate to the results/detail route, open the record, or fetch the export. The output is often not on the page where you triggered it.
2. **Judge the content against the inferred outcome.** Is it **non-empty**, **on-topic**, **coherent**, and **specific to the inputs** — or is it empty, an error object, a placeholder, stale, or generic filler? Cross-check persisted fields: after "run research," the client's keywords/competitors should be populated; after "generate," posts should exist and read sensibly; after "save", re-opening shows your values.
3. **A billed/confirmed action with empty or irrelevant output is a real (often high-severity) bug** — it is invisible at the gate and is exactly what this skill exists to catch. Flag it with the evidence (what you expected vs. the empty/garbage artifact you found).

For the deterministic layer, a `flow` step can assert `content_contains` / `content_absent` (e.g. result panel shows the entity, and does **not** say "No results"/"Error") — but those are only a coarse net. **Whether the produced content is genuinely good is your semantic judgment, and it is not optional for output-producing actions.**

> Real example (2026-07-12, content-jumpstart): "Run research" returned `200` and deducted 200 credits — step "passed." Reading the produced client showed `keywords: []`, `competitors: []`, `results: total 0` — the tool billed for research and produced nothing. Only reading the artifact caught it.

### 3c. Feature-completeness audit — determine & document what is NOT built (mandatory)

**A real QA pass enumerates the app's incomplete and unimplemented features, not just the bugs in the built ones.** Users judge a product by the gap between what it advertises and what actually works, so produce an explicit inventory of every feature that is missing, stubbed, disabled, or broken. Gather it from *all* of these signals, not one:

1. **Disclosed markers** — every `incomplete` entry from the explore snapshot plus any "Coming Soon", "Beta", "WIP", "Preview", "under construction", or roadmap/changelog copy in the UI. If the app has a features/roadmap/what's-new page, read it — it is the app's own list of what isn't done.
2. **Disabled or dead controls** — greyed-out buttons, `disabled`/`aria-disabled` elements, `dead: true` links, menu items that no-op.
3. **Undisclosed stubs (higher severity)** — a route or feature that *looks* live (in nav, not labeled Coming Soon) but renders an error/crash, an empty placeholder, "no data" with no way to get data, or a control whose handler is never wired. These are worse than a labeled Coming Soon because the app claims the feature exists.
4. **Advertised-live vs. actual** — cross-check any in-app "what's included / working now" list against real behavior. **A feature the app lists as *live* that actually crashes or no-ops is a top finding** — the disclosure is false. (Conversely, a feature labeled Coming Soon that already works is a stale label — worth a low note.)
5. **Paid/credit-gated incompleteness** — a Coming Soon or stub behind a CTA that still charges, navigates to a dead checkout, or occupies a primary action slot deserves elevated severity.

Report a dedicated **"Incomplete / unimplemented features"** section that lists each item with: name, where it surfaces, how it's gated (disclosed Coming Soon vs. undisclosed stub vs. crash), and user impact. Separate *disclosed-incomplete* (usually low — honest) from *undisclosed/broken* (the real problems). This inventory is a required part of a full/thorough review, alongside the issue list.

### 3d. Accessibility audit — can assistive-tech users actually use this? (mandatory for a full/thorough review)

Test for the user who reaches the app through a screen reader, by keyboard only, or at high zoom. This has a **deterministic half** (objective WCAG facts, in the engine) and an **advisory half** (whether the experience is *coherent*, your judgment) — the same split as the rest of the skill.

**Deterministic — run the audit (authoritative, objective).** Every `explore` snapshot already embeds an `accessibility` report; to audit a specific route (including behind auth), run:

```bash
python -m engine.cli a11y --url <URL> [--session .qa/session.json]
```

It returns `violations[]` — one aggregated entry per failed rule, each with `impact` (`critical`/`serious`/`moderate`/`minor`), a `help` string (the rule + the fix), a `count`, and example `nodes` (`selector` + `snippet`) — plus `counts` by impact. The rules are a curated, low-false-positive WCAG A/AA subset: `image-alt` (informative `<img>` with no alt), `control-name` (form field with no programmatic label — a placeholder does **not** count), `button-name` / `link-name` (control announces as just "button"/"link"), `html-has-lang`, `document-title`, `heading-order` (no `<h1>` or a skipped level), `positive-tabindex`, `duplicate-id`. These are facts (a missing `alt` *is* missing), so they are **deterministic** issues — emit each as `category: "accessibility"`, `source: "deterministic"`. Map impact → severity: `serious`/`critical` → **high** (it blocks the user — an unlabeled login field is unusable by a screen reader), `moderate` → **medium**, `minor` → **low**. Run the audit on each **distinct** page/state (landing, the authed core, a form-heavy view, any modal/wizard step), not just the entry URL — like the backend model, coverage is bounded by the states you visit.

**Advisory — judge coherence (your semantic call, like §3/§3b).** The audit proves a name/attribute is *present or absent*; it can't tell you the experience makes sense. That is yours:

- **Keyboard-only journey.** Re-drive a core journey using only keyboard actions (`press` Tab / Shift+Tab / Enter / Space / Escape). Can you reach and operate every control needed to finish the goal? Is a visible focus indicator present at each step? Does focus get **trapped** (a modal you can't Tab out of, or that never received focus on open), or **lost** (after a route change focus drops to `<body>` and a screen-reader user is stranded)? A journey that a mouse user completes but a keyboard user cannot is a **high**-severity finding.
- **Screen-reader coherence.** Read the audit's accessible names and the snapshot's `dom_outline` (roles + landmarks + headings) as if heard top-to-bottom. Do the names actually *describe* their targets ("Delete invoice #42", not "🗑️"/"button")? Do landmarks and heading levels form a sensible outline to navigate by? A control that is technically *named* but whose name is useless is still a real finding — advisory, `source: "ai"`.

**Report an "Accessibility" section** in the results: the deterministic violations grouped by impact (with counts and example selectors), then your keyboard/screen-reader findings. As always, deterministic violations are authoritative; never downgrade one because the page "looks fine" — it doesn't, to the user you're testing for. If you skipped the audit on some states (cap, time), say which.

### 4. Report

Assemble a results object and render it. **When you fanned out (Orchestration §), this is the serial merge step:** union the subagents' fragments into one `issues[]`, dedup issues raised by more than one subagent (same title+url → one), renumber to contiguous `issue-NNN`, and render **once** — subagents never render their own report. **Save every report under the repository-root `reports/` directory** (create it if missing), in a per-run folder named `<site-slug>-<YYYY-MM-DD>`:

- **`site-slug`** — the page `<title>` from the explore snapshot, slugified (lowercase, spaces/punctuation → `-`); fall back to the target hostname when the title is empty or generic. E.g. title "Operator Dashboard" on `content-jumpstart-backend.onrender.com` → prefer the hostname slug `content-jumpstart-backend`.
- **`YYYY-MM-DD`** — today's date.
- From the skill dir, the repo-root `reports/` is `../../../reports/`.

```bash
# e.g. site-slug=content-jumpstart-backend, date=2026-07-12
python -m engine.cli report --input results.json --output ../../../reports/content-jumpstart-backend-2026-07-12
```

This writes `report.md` / `report.json` / `issues.json` (+ `screenshots/`) into that folder, so reports persist and accumulate per site/date instead of living in a temp dir. If a run for the same site+date already exists, append a short suffix (`-2`, `-am`) rather than overwriting a prior report.

`results.json` shape:

```jsonc
{
  "metadata": {"run_id": "...", "target_url": "...", "engine": "chromium", "actions_total": 6, "actions_capped": false},
  "evidence": [ /* the act bundles, optional */ ],
  "issues": [{
    "id": "issue-001", "title": "...", "severity": "critical|high|medium|low",
    "category": "functional|console|network|navigation|content",
    "source": "deterministic|ai", "url": "...", "description": "...",
    "gate": { /* the failing gate object, for deterministic issues */ },
    "ai_verdict": {"verdict": "inappropriate", "confidence": "medium", "reasoning": "..."},
    "screenshot": "screenshots/...png"  // relative to --output dir, so image links resolve inside the saved report folder
  }]
}
```

Then give the user an in-session summary: what was tested, issues found (deterministic vs advisory), and anything skipped (cap, destructive, or errored).

## Security sweep — endpoint auth enforcement (thorough audits)

On a thorough audit, in addition to the UI pass, verify the backend actually enforces authentication/authorization. Run it with the engine: `python -m engine.cli sweep --url <BASE> --token-env <VAR>`. This is the one HTTP-level check (not browser-driven).

**Safe by default.** The sweep probes only read-only (GET) endpoints unless you pass `--include-mutating`; probing a write verb sends a *real* request that would **execute** on an unprotected endpoint (a live `DELETE`/`cache-clear`). Only add `--include-mutating` against a **test** target you own. The result's `coverage` field says whether the run was complete: **a safe (read-only) sweep with zero findings is NOT a clean bill of health** — if `coverage` is `PARTIAL`, unauthenticated *writes* (the highest-risk bypasses) were never tested, so **report that explicitly** ("read-only sweep: N write endpoints not probed — re-run with `--include-mutating` on a test target"). Never present a partial sweep as "no auth issues." Manual method:

1. **Enumerate the API surface.** If the app exposes an OpenAPI spec (`/openapi.json`, `/docs`), pull every `(method, path)`. Otherwise collect the endpoints observed in the network logs during the UI pass.
2. **Call each endpoint twice** — once with **no** `Authorization` header, once with a valid token — and compare status. Substitute dummy values for path params (`{id}` → a real id from an authenticated list call, or a throwaway). Withhold mutating verbs on any target you don't own.
3. **Flag any endpoint that should be protected but returns 2xx (or runs to a 500) without a token** — especially state-mutating (`POST`/`PUT`/`PATCH`/`DELETE`) and anything exposing data or internals. Watch for **duplicate routes**: a protected action re-exposed under a second unauthenticated path (e.g. an authed `/api/cache/clear` twinned by an open `/api/health/cache/clear`). Give admin/user-management, database backup/restore, privacy delete/anonymize, credit adjust/grant, and profiling/health-internal endpoints the closest look — an unauthenticated hit there is critical.
4. **Legitimately public** (do not flag): `login`/`register`/`refresh`, health *liveness/readiness* probes, static assets, stateless public calculators, and signature-gated webhooks. On a test instance you may confirm an exposure by actually invoking it; note exactly what you triggered.

Report auth findings as `deterministic` issues with `category: "security"`.

## Safety — destructive actions (mandatory)

**Never fire an irreversible interaction autonomously** — payments, deletes, sending mail, account changes, or submitting a form the snapshot marks `destructive: true` (e.g. a newsletter/signup — it creates a real record).

There is no engine allowlist; this judgment is yours. When you classify a candidate as destructive, **ask the user for explicit confirmation before running `act` on it** — the session's Claude Code permission settings are the backstop. Read-only/idempotent actions (navigation, reading state) run freely.

## Notes & current limitations

- **New-tab/external links** are captured: `act` follows `target="_blank"` popups and reports the opened page's status; a ≥400 fails `opened_pages_ok`. This is how broken external links are caught.
- **Accessibility (`a11y` subcommand + `explore`'s `accessibility` field):** a deterministic WCAG A/AA subset (missing alt/label/name, no lang/title, heading-order, positive tabindex, duplicate id) — objective violations, so authoritative. It is a curated low-false-positive net, **not** a full axe-core scan, and it does **not** cover color-contrast, reflow/zoom, or motion; the keyboard-nav and screen-reader *coherence* judgment is the agent's, not the engine's (§3d).
- `act` is **stateless per action** — one fresh browser navigating to `--url` then performing the single action; it has no notion of a run. Use it for isolated checks. It (like `explore` and `flow`) accepts `--session <bundle>` to start already authenticated from a saved session (§3a).
- **Auth session persistence (`--session` / `--save-session` / `--user-agent`):** `flow --save-session <file>` writes the context's cookies + user-agent after a login; `explore`/`act`/`flow --session <file>` seed a new context from it, so runs are authenticated without re-login. **Establish once, replay many** — the standard way to test an authenticated app without tripping auth rate limits or bot challenges. Pin the SAME `--user-agent` for login and every replay (tokens are UA+IP-fingerprint-bound). See §3a.
- **For anything stateful (login → wizard → generate → export), use the `flow` subcommand** — an ordered step list run in one persistent browser context, one evidence bundle + gate + optional per-step `assert` each, halting at the first failed step (`--continue-on-fail` to override). Steps support `settle_ms` (extra idle wait — long async runs like an LLM generation can take minutes; size it to the work) and `await_response` (`{method, path_contains, timeout_ms}` — block until a specific response lands). Secrets are referenced by env var (`{"env":"VAR"}`), never inlined. This is the primary tool for real journeys (§3a).
  - **Client-driven multi-step actions keep the browser busy.** If one click kicks off a client-side loop (e.g. "Generate Report" firing one `/run` per selected tool in sequence), the context must stay open until it finishes — size `settle_ms` to cover the *whole* batch, or the later iterations are aborted (and may still have been billed). When a batch is long, run the `flow` in the background and then poll the results view in a separate `flow`.
- Orchestration, the 15-action cap, ranking, and **fanning work out across concurrent subagents** (Orchestration §) are your responsibility (this file), not engine flags. The engine gives you independent one-shot processes; parallelism comes from running many of them at once in subagents, not from an engine flag.

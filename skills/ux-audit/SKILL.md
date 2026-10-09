---
name: ux-audit
description: Browse and test a website's UI/UX in the user's real Chrome. A fast Jev browser agent walks through realistic user scenarios (search, sign-up, checkout up to payment, posting a listing up to review, finding pricing or contact info), while scripts collect per-step screenshots, what each action actually did, console and network errors, document metrics, dead clicks, confusing controls, plus automated checks for accessible names, tap-target size, contrast, mobile layout and heading structure. Every scenario gets a verdict (passed, failed, blocked, inconclusive) checked against the start state, with a cause (site, agent, scenario, environment, unknown). Claude then writes a prioritized UX report. Use this whenever the user wants to test, audit, review, QA or smoke-test a site's usability, accessibility, user flows or mobile experience, check whether users can complete a task on a URL, compare UX before/after a change, or "click through" a site to find problems — even if they don't say "UX audit".
---

# UX audit with Jev

A Jev agent uses the site like a first-time visitor: it reads visible, labelled controls and decides one step at a time. Where *it* hesitates between controls, clicks something that does nothing, or gets stuck is a hint about where people may struggle, but only a hint. The scripts record evidence and give each scenario a verdict from checks the code performs. Your job is to design scenarios with real postconditions, separate site defects from agent limits, and report only what the evidence supports.

The agent code lives in the `jev-ultrafast` repository this skill belongs to. `scripts/run.sh` finds it through the symlink, so call the script by its path inside this skill directory. (`find` does not follow that symlink without `-L`.)

## How the agent works (what to expect)

- **Models.** Every decision is one call to `typesafe-ai/jev` on Vercel AI Gateway's TypeSafe-compatible endpoint (`/typesafe/v1/systemone`). Jev picks an operation (click, type, select, scroll, wait, done, blocked) and a target from a numbered list of observed controls. Only when the operation is "type text" does `inception/mercury-2.5` (AI Gateway chat endpoint) write the value. There is no hidden fallback model. The code never runs model output as selectors or scripts.
- **Observation.** Visible controls with role, accessible name, value, **region** (`header › search`, `main › form "…"`, `dialog "…"`) and `in_view`. Controls below the fold are included and scrolled into view when used. Nested scroll containers get their own scroll actions. While a modal dialog is open, only its controls are offered. Not supported: file upload, iframes, shadow DOM, canvas, drag & drop, pop-up windows, CAPTCHAs.
- **After every action** the agent waits for the effect (DOM changes, URL change, finished requests, `aria-busy`), bounded: about 1 s if nothing happens, up to 3 s (5 s for data-changing clicks). Each step records a `result`: `navigated`, `changed`, `transient` (something appeared and vanished, e.g. a toast), `none` or `timeout` (still busy, outcome unknown).
- **Data-changing clicks** (submit buttons, and labels like add to cart, publish, delete, send, pay, in several languages; a heuristic) run at most once. A second run happens only if the first had a confirmed effect and the form inputs changed since. A click with an unknown effect (`none`, `timeout`) is set aside, and the agent waits or re-observes instead of repeating it.
- **Loops.** The same control tried twice without reaching a new page state is set aside, and Jev is told so. The next attempt without progress ends the scenario as `no_progress_loop`, with the last steps as evidence. Budgets: `max_steps` (default 30) and `max_seconds` (default 180).
- **`forbid`** in a scenario is a regex over control labels. Matching controls are never offered and never executed. Use it for anything the scenario must not do (`"publicar|publish|pay|delete"`).

## 0. Preconditions

- Chrome is running with remote debugging allowed (`chrome://inspect/#remote-debugging`). If the run fails with `chrome-not-running` or `remote-debugging-setup`, open that page with `open -a "Google Chrome" "chrome://inspect/#remote-debugging"` and ask the user to enable it and click **Allow**.
- `AI_GATEWAY_API_KEY` is set in the repo's `.env` or the environment. Don't read `.env` to check: a missing key fails the first decision with a clear error.
- The agent runs in the user's **real Chrome profile**, with their sign-ins and cookies. That makes logged-in flows testable, and it is why the safety rules matter. By default each scenario opens its own background tab, in the same Chrome profile as any tab already open on the site, and closes it afterwards.

### Signed-in flows

- Mark the scenario `"requires_auth": true`. Sign-in state is judged only from visible UI: a visible password field means signed out. The script never reads cookies, storage or tokens, and neither should you.
- If a password field is visible at the start, the scenario is not run and gets `blocked / environment`. Ask the user to sign in in Chrome, then rerun.
- A URL like `/login?redirect=…` only says which page is showing, not *why* the user is signed out. Don't diagnose sessions from it.
- If the user is signed in in their tab but the tool's new tab still shows a login form, the site may keep the session per tab. Rerun with `--reuse-tab`, which drives the user's own open tab for that site.
  - Tell the user first: their tab will navigate. It stays open, ending on the run's final page.
  - Emulation, the injected scripts, request counters, console hooks and observers are removed afterwards.
  - Nothing else is restored: the URL, form drafts and filters stay as the run left them.
- Never type the user's password. If signing in must be part of the scenario, use a test account the user provides.
- The mobile pass always uses fresh tabs. They share cookies with the profile, but per-tab state (a sessionStorage sign-in, a cart) is not inherited. Don't present mobile results as a signed-in mobile test unless the page shows a signed-in state.

## 1. Safety: what scenarios may do

Write goals that stop *before* anything irreversible or outward-facing:
- no payment and no order;
- no sending messages or reviews;
- no publishing and no deleting;
- no changing account or security settings;
- no real sign-ups with the user's identity.

Put the stopping point in the goal ("…stop when the review step is shown") **and** in `forbid`. If the user explicitly wants a flow that completes such an action, confirm that specific action with them. Approval for one test object does not cover another. Prefer staging or a test account.

Use made-up test data (`test@example.com`, titles starting with `TEST`), never the user's personal data. Keep runs small: this is an audit, not a load test.

A scenario that never publishes can still leave server-side drafts, saved filters or changed preferences behind. Each result lists typed values and data-changing actions under `left_state`. Report them. Delete only objects you can unambiguously identify as created by this test, and only within what the user allowed.

## 2. Plan scenarios

Find out the site, who uses it, and which tasks matter. Check what the site actually offers before writing goals, for example by reading its category list. A goal the site was never meant to support (a category it doesn't have) is a `scenario` problem, not a site defect, until requirements show otherwise.

Propose 3–6 scenarios:
- the **primary task**;
- **navigation and discovery**;
- a **form**, including one **error path**;
- whatever the user is worried about.

Start at a deep URL when only one step matters.

Each scenario needs **postconditions that are false at the start and true only after the task**. The script evaluates every check at the start and at the end:
- a check already true at the start proves nothing;
- if all checks were already true, the verdict is `inconclusive / scenario`;
- Jev's own DONE never counts as success.

```json
{
  "site": "https://shop.example.com",
  "mobile": true,
  "scenarios": [
    {"name": "add-to-cart",
     "url": "https://shop.example.com/p/waterproof-jacket",
     "goal": "Add the waterproof jacket in size M to the cart once.",
     "forbid": "checkout|pay|buy now",
     "expect": {"text_seen": ["Added to cart"]}},
    {"name": "return-policy",
     "goal": "Find out how many days I have to return an item.",
     "expect": {"url_changed": true, "text_contains": ["days to return"]}},
    {"name": "post-listing-to-review",
     "url": "https://market.example.com/post",
     "requires_auth": true,
     "forbid": "publish|publicar",
     "goal": "Prepare a test listing titled 'TEST lamp' for 1 EUR in Madrid and stop at the review step.",
     "expect": {"text_contains": ["TEST lamp"]}, "max_steps": 25}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `name`, `goal` | Required. |
| `url` | Start page; defaults to `site`. |
| `expect.url_contains`, `expect.url_changed`, `expect.title_contains` | URL and title checks. |
| `expect.text_contains`, `expect.text_absent` | Whole page text at the end. |
| `expect.text_seen` | Appeared after any action, for feedback that disappears again (toasts). |
| `requires_auth`, `forbid`, `max_steps`, `max_seconds`, `reuse_tab` | Per scenario. |
| Top level: `mobile: false`, `reuse_tab: true` | Skip the mobile pass; equivalent to `--reuse-tab`. |

## 3. Run

```bash
~/.claude/skills/ux-audit/scripts/run.sh scenarios.json --out ux-audit-<site>-<date>
~/.claude/skills/ux-audit/scripts/run.sh --url URL --goal "..." --out DIR   # quick single task
```

Put `scenarios.json` and the output folder in the user's project, or in the scratchpad when there is none. For a rerun, use a new folder (`…-run2`) and keep the same scenarios, so before/after comparisons stay meaningful.

Typical durations:
- a scenario takes 3–60 s;
- a mobile page about 3 s;
- 30 s+ when the agent explores.

Options:
- `--reuse-tab`, `--no-mobile`;
- `--max-steps N`, `--max-seconds N` (scenario values win);
- `--max-mobile-pages N`, default 6.

Output:
- **`summary.md`.** Read it first. It contains:
  - a verdict table (outcome, cause, agent stop, actions, actions without progress, ambiguous choices, errors, time);
  - per scenario: the checks (start → end) and a step trace with region, result and wait reason;
  - what was set aside and why, and the deduplicated errors;
  - final document metrics, what was left behind, and model use (calls, tokens, and gateway-reported cost when reported);
  - per page: issues at 1120px and 390px, how the page was reached, readiness, whether a login form was visible.
- **`results.json`** (schema 2). Everything, including located examples per issue (`el`, `text`, `rect`), decision alternatives per step, stop evidence, and `left_state`.
- **Screenshots.**
  - `scenarios/<name>/step-NN.jpg`: the viewport after each action;
  - `final.jpg` and `final-full.jpg`: full page, supplementary;
  - `error.jpg`: on a crash;
  - `mobile/*.jpg`.

## 4. Interpret the evidence

Look at screenshots before concluding anything: `final.jpg` for every scenario, the step where things went wrong, and every mobile screenshot. A full-page screenshot is extra evidence, not a substitute for the checks.

**Verdicts** come from the code:

| Outcome | Meaning |
| --- | --- |
| `passed` | All checks true at the end, and at least one of them changed from the start. |
| `failed` | The agent said DONE, but the checks are not met. |
| `blocked` | It stopped first: loop, budget, BLOCKED, error, signed out, or an upload the agent can't do. |
| `inconclusive` | No checks were given, or they were already true at the start. |

**Causes** the script sets itself, only where the evidence proves them:
- `environment`: Chrome or Gateway errors, or signed out at the start;
- `agent`: a budget ran out, an execution error, or no progress on a page that asks for a file upload;
- `scenario`: the goal was already satisfied at the start;
- everything else is `unknown`.

You classify `unknown` in the report, with evidence. Allowed causes are `site`, `agent`, `scenario`, `environment`, or `unknown` if you can't tell. Call it `site` only when a person would hit the same problem. If unsure, write "needs manual check".

| Signal | Often means | Check first |
| --- | --- | --- |
| `none` after a click, no progress | Dead click or silent validation | Step screenshot: did an error message appear that the step didn't register? |
| `transient` | Feedback that vanishes (toast) | Fine if the message was clear; `text_seen` verifies it. |
| `timeout` on a data-changing click | Slow server; the outcome is unknown | Did success appear later? The agent never repeats it. |
| Ambiguous target choice (< 0.6, different names or regions) | Competing controls, e.g. a header filter named like a form field | Same name in different regions is an accessibility finding (WCAG 2.4.6/2.4.4) even if people can tell them apart visually. |
| Agent wandered into header or other regions after a wall | The agent exploring | Find the first step without progress: that is the real blocker. |
| Many decisions for few actions | Page changed under the agent | Late rendering, layout shift. |
| Console errors, failed resources (deduplicated) | Broken features | Did they affect the flow? 401s on secondary APIs while signed in deserve a manual check, not a verdict. |

**Automated page checks.** These are heuristics, not a full WCAG audit; confirm with the examples first.
- `missing_accessible_name`, `unlabelled_field`, `placeholder_only_label`, `image_missing_alt`, `clickable_div_without_role`: WCAG 1.1.1, 1.3.1, 4.1.2.
- `target_below_24px`, counted only when crowded: WCAG 2.5.8 AA. `target_below_44px_mobile` is a platform guideline.
- `low_contrast_text`: WCAG 1.4.3. Text over images or gradients is skipped.
- `horizontal_overflow`, or "layout is N px wide on a 390px phone".
- `vague_link_text`, `same_name_different_target`. Nested wrappers of one control are not counted.
- `structure`: h1, heading levels, `lang`, viewport meta, `<main>`.

**Metrics.** Lab values from one run in the user's Chrome and network. Never present them as field Core Web Vitals.
- **Document metrics** belong to the URL that loaded the document. CLS is the standard largest session window; LCP is `unavailable` with a reason when the browser did not report it.
- **Client-side route changes** get only "route diagnostics": layout shift since the route change and long tasks. These are not CWV.
- Good LCP ≤ 2500 ms; CLS ≤ 0.1; TTFB ≤ 800 ms.

## 5. Write the report

Follow `references/report.md`. Save it as `ux-report.md` in the output folder and reference screenshots by relative path. Always state what was left behind on the site and in the user's tab.

When the report is meant for a team, offer to publish it as a shareable page.

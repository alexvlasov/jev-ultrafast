# UX report format

Write for the people who will fix things. A product owner reads the top; a developer jumps to a finding and needs the element, the evidence and the fix. Match the user's language (e.g. Ukrainian if they wrote in Ukrainian), but keep element names, code, statuses and WCAG references as they are.

## Facts, inferences, checks

Mark every claim as one of these:

- **Verified**: shown by a check, a screenshot or a server/DOM fact. Examples: "the step-4 error text says a photo is required", "the server received one request".
- **Inferred**: likely, but not demonstrated. Example: "401 on chat counters while signed in suggests some APIs don't see the session". Say "suggests" or "probably".
- **Needs manual check**: anything you could not confirm.

Never upgrade an inference to a finding because it fits a story. If an earlier report or message stated something the evidence no longer supports, correct it explicitly.

## Cause per scenario

Use the script's outcome and cause. Where the script says `unknown`, assign one of these:

| Cause | When |
| --- | --- |
| `site` | A person would hit the same problem: missing feedback, a broken control, a misleading label, an error. |
| `agent` | Outside the agent's abilities, or its own wandering. File uploads, iframes, CAPTCHAs, budgets. |
| `scenario` | The goal does not match what the site offers or requires, or the postconditions were weak. |
| `environment` | Signed out, network, Chrome, Gateway. |
| `unknown` | The evidence does not decide it. Say what would. |

## Severity (site findings only)

- **Critical**: a core task cannot be completed, or data/money is at risk.
- **High**: the task is possible, but many users will fail, give up, or be excluded.
- **Medium**: noticeable friction, or a WCAG AA failure on secondary paths.
- **Low**: polish and best practice.

Rate by impact on real users and how often they hit it, not by how many elements a check counted. Agent and scenario problems get no severity. They go under "Limits of this audit".

## Structure

```markdown
# UX audit: <site>  ·  <date>

## Summary
Two to four sentences: can users complete the key tasks, the biggest verified problems, the overall state.
Table: Scenario · Outcome (passed / failed / blocked / inconclusive) · Cause · Time · Main point.

## Findings
### [High] <Short problem statement in user terms>
- **Where:** page / scenario and step, element ("Continuar" in `main › form`, at x,y)
- **Evidence:** what happened, step result, check or metric, screenshot path. Verified or inferred?
- **Impact:** who is affected and how
- **Fix:** concrete change (label text, markup, CSS, behavior)
- **Ref:** WCAG criterion or heuristic, if applicable

…ordered by severity, then by how central the flow is.

## Needs manual check
Observations the evidence suggests but does not prove.

## Accessibility & mobile checks
Compact table per page: check · desktop · mobile · representative example. These are automated heuristics.
Note the mobile pass conditions: fresh tab, 390px, signed-in state as seen on the page.

## Performance
Document metrics per document-load page (LCP, CLS session window, TTFB, long tasks, failed resources). Separately,
route diagnostics for client-side routes. One lab run on the user's machine; not field Core Web Vitals.

## What worked well
Short list. It tells the team what not to break.

## Limits of this audit
- What the agent could not operate.
- Scenarios stopped on purpose (`forbid`, before publishing or paying).
- The signed-in state used.
- That this was a single run.
- Corrected earlier conclusions, if any.

## Left behind
- Per scenario: the tab (tool tab closed, or the user's tab and its final URL).
- Values typed.
- Data-changing actions and their results.
- Possible server-side drafts.
- Anything you cleaned up, and how you identified it.
```

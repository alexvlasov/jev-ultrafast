"""Run user scenarios with the Jev agent and collect UX evidence.

    run.sh scenarios.json --out DIR
    run.sh --url URL --goal "A user goal" --out DIR

scenarios.json:
    {"site": "https://example.com", "mobile": true, "reuse_tab": false,
     "scenarios": [{"name": "search", "goal": "...", "url": "optional start URL", "requires_auth": false,
                    "expect": {"url_contains": "/search", "url_changed": true, "title_contains": "Results",
                               "text_contains": ["results"], "text_absent": ["No results"],
                               "text_seen": ["Added to cart"]},
                    "max_steps": 25, "max_seconds": 180}]}

Writes DIR/results.json (schema 2, everything), DIR/summary.md (read first) and screenshots.
Outcomes: passed | failed | blocked | inconclusive. Causes: site | agent | scenario | environment | unknown.
The script sets only causes it can prove; everything else is "unknown" for the report author to classify.
"""

import argparse
import base64
import json
import re
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urldefrag

from jev_ultrafast import Agent, Browser
from jev_ultrafast.browser import StalePage

SCHEMA_VERSION = 2
HERE = Path(__file__).resolve().parent
CAPTURE = (HERE / "capture.js").read_text()
CHECKS = (HERE / "checks.js").read_text()
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Mobile Safari/537.36"
)
LOW_CONFIDENCE = 0.6
FACTS = """({url: location.href, title: document.title, text: (document.body?.innerText || '').slice(0, 200000),
  document_id: performance.timeOrigin,
  file_inputs: document.querySelectorAll('input[type=file]').length,
  login_form_visible: [...document.querySelectorAll('input[type=password]')]
    .some(e => e.checkVisibility?.({checkOpacity: true, checkVisibilityCSS: true}))})"""
AGENT_LIMITS = {"time_budget", "step_limit"}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "scenario"


def save_jpg(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(data))
    return str(path)


def safe_eval(browser, expression):
    for _ in range(5):
        try:
            return browser.evaluate(expression)
        except (StalePage, RuntimeError):
            time.sleep(0.2)
    return None


def full_page_screenshot(browser, path):
    """Supplementary evidence of content below the fold; never a substitute for DOM checks."""
    try:
        size = browser.call("Page.getLayoutMetrics")["cssContentSize"]
        height = min(size["height"], 6000)
        data = browser.call("Page.captureScreenshot", format="jpeg", quality=60, captureBeyondViewport=True,
                            clip={"x": 0, "y": 0, "width": size["width"], "height": height, "scale": 0.5})["data"]
        return save_jpg(path, data)
    except (RuntimeError, KeyError):
        return None


def top(probabilities, labels=None, n=3):
    ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return [{"option": (labels or {}).get(k, k), "p": round(v, 3)} for k, v in ranked]


def decision_summary(decision):
    """Alternatives per decision. Near-ties between differently named targets show where the UI reads ambiguously."""
    operation = decision["operation"]
    labels = {}
    question = decision["request"]["questions"].get(operation.lower() + "_target")
    if question:
        labels = {k: v["element"] + (f" ({v['region']})" if v.get("region") else "")
                  for k, v in question["criteria"].items()}
    op_p = decision["operation_probabilities"].get(operation, 0)
    target_p = decision["target_probabilities"].get(decision["target"], 1) if decision["target"] else 1
    alternatives = top(decision["target_probabilities"], labels) if question else []
    # Two observed nodes with the same label and region (a suggestion row and its link) are one choice for a user.
    distinct = [re.sub(r"^\[[^\]]+\]\s*", "", a["option"]) for a in alternatives[:2]]
    same_label_tie = len(distinct) == 2 and distinct[0] == distinct[1]
    return {
        "step": decision.get("step"),
        "operation": operation,
        "operation_p": round(op_p, 3),
        "target": labels.get(decision["target"]) if decision["target"] else None,
        "target_p": round(target_p, 3),
        "operation_alternatives": top(decision["operation_probabilities"]),
        "target_alternatives": alternatives,
        # Only target near-ties say something about the interface; DONE-vs-BLOCKED doubt is about the goal.
        "ambiguous": bool(question) and target_p < LOW_CONFIDENCE and not same_label_tie,
        "uncertain_stop": operation in {"DONE", "BLOCKED"} and op_p < LOW_CONFIDENCE,
        "latency_ms": decision["latency_ms"],
        "elapsed_ms": decision["elapsed_ms"],
    }


def evaluate_expectations(expect, baseline, final, observed=()):
    """Each check is evaluated at the start and at the end; only a change shows the scenario did something.
    observed: page texts seen after each action, for feedback that disappears again (toasts)."""
    checks = []

    def add(name, at_start, at_end):
        checks.append({"check": name, "at_start": at_start, "at_end": at_end, "passed": at_end})

    if "url_contains" in expect:
        add(f"url contains {expect['url_contains']!r}", expect["url_contains"] in baseline["url"],
            expect["url_contains"] in final["url"])
    if expect.get("url_changed"):
        add("url changed", False, urldefrag(final["url"])[0] != urldefrag(baseline["url"])[0])
    if "title_contains" in expect:
        needle = expect["title_contains"].lower()
        add(f"title contains {expect['title_contains']!r}", needle in baseline["title"].lower(),
            needle in final["title"].lower())
    for needle in expect.get("text_contains", []):
        add(f"text contains {needle!r}", needle.lower() in baseline["text"].lower(),
            needle.lower() in final["text"].lower())
    for needle in expect.get("text_seen", []):
        add(f"text seen during the run {needle!r}", needle.lower() in baseline["text"].lower(),
            any(needle.lower() in text.lower() for text in [*observed, final["text"]]))
    for needle in expect.get("text_absent", []):
        add(f"text absent {needle!r}", needle.lower() not in baseline["text"].lower(),
            needle.lower() not in final["text"].lower())
    return {
        "checks": checks,
        "all_passed": bool(checks) and all(c["passed"] for c in checks),
        "already_satisfied_at_start": bool(checks) and all(c["at_start"] for c in checks),
        "changed_from_baseline": any(c["passed"] and not c["at_start"] for c in checks),
    }


ENVIRONMENT_ERRORS = ("Model connection failed", "Model provider returned HTTP", "Model unavailable",
                      "chrome-not-running", "remote-debugging", "daemon", "AI_GATEWAY_API_KEY", "TEXT_MODEL_API_KEY")


def error_source(error):
    """Who failed: the environment (network, gateway, Chrome) or the agent's own execution."""
    message = f"{type(error).__name__}: {error}"
    if isinstance(error, (TimeoutError, ConnectionError)) or any(m in message for m in ENVIRONMENT_ERRORS):
        return "environment"
    return "agent"


def classify(result):
    """Outcome from postconditions, not from the agent's own DONE. Causes only where the evidence proves them."""
    agent_status, stop = result.get("status"), result.get("stop_reason")
    verification = result.get("verification") or {}
    auth = result.get("auth") or {}
    if result.get("error"):
        return "blocked", result.get("error_source", "unknown"), result["error"]
    if auth.get("required") and auth.get("login_form_visible_at_start"):
        return "blocked", "environment", "sign-in required, but a password field was visible at the start"
    if not verification.get("checks"):
        return "inconclusive", "unknown", f"no postconditions defined; the agent's own status was {agent_status}"
    if verification["already_satisfied_at_start"]:
        return "inconclusive", "scenario", "every postcondition was already true before the first action"
    if verification["all_passed"] and verification["changed_from_baseline"]:
        note = f"; the agent then stopped with {stop}" if stop else ""
        return "passed", None, "postconditions met and at least one changed from the baseline" + note
    if auth.get("required") and auth.get("login_form_visible_at_end"):
        return "blocked", "environment", "a password field is visible at the end: not signed in"
    if result.get("file_upload_pending"):
        where = result["file_upload_pending"]
        steps = f" (no progress at steps {where['steps']})" if isinstance(where, dict) and where["steps"] else ""
        return "blocked", "agent", f"the page asks for a file upload{steps}; the agent cannot upload files"
    if stop in AGENT_LIMITS:
        return "blocked", "agent", f"stopped by the {stop} budget before the postconditions were met"
    if agent_status == "done":
        return "failed", "unknown", "the agent reported done, but postconditions are not met"
    reason = stop or ("agent chose BLOCKED" if agent_status == "blocked" else agent_status)
    return "blocked", "unknown", f"stopped ({reason}) before the postconditions were met"


def dedupe(events):
    groups = {}
    for e in events:
        key = (e["kind"], e["message"][:200])
        if key not in groups:
            groups[key] = {**e, "count": 0, "first_after_step": e.get("after_step")}
        groups[key]["count"] += 1
    return sorted(groups.values(), key=lambda g: -g["count"])


def usage_totals(state):
    decisions, text_calls = state.get("decisions", []), state.get("text_calls", [])
    costs = [d["usage"]["cost"] for d in decisions if "cost" in d.get("usage", {})]
    text_costs = [t["usage"]["cost"] for t in text_calls if "cost" in t.get("usage", {})]
    return {
        "decision_model": decisions[0]["model"] if decisions else None,
        "decision_calls": len(decisions),
        "decision_input_tokens": sum(d.get("usage", {}).get("input_tokens", 0) for d in decisions),
        "decision_output_tokens": sum(d.get("usage", {}).get("output_tokens", 0) for d in decisions),
        "decision_latency_ms": sum(d.get("latency_ms", 0) for d in decisions),
        "text_model": text_calls[0]["model"] if text_calls else None,
        "text_calls": len(text_calls),
        "text_latency_ms": sum(t.get("latency_ms", 0) for t in text_calls),
        # Only what AI Gateway itself reported; None when it reported nothing.
        "gateway_cost_usd": round(sum(costs + text_costs), 6) if costs or text_costs else None,
        "gateway_cost_complete": len(costs) == len(decisions) and len(text_costs) == len(text_calls),
    }


def audit_page(browser, audited, scenario, auth_required):
    """Run page checks once per URL, after the interface is ready, and record how the page was reached."""
    ready = browser.ready(cap=4000)
    facts = safe_eval(browser, FACTS)
    if not facts:
        return
    key = urldefrag(facts["url"])[0]
    if key in audited:
        return
    checks = safe_eval(browser, CHECKS)
    if not checks:
        return
    vitals = checks.pop("vitals", None) or {}
    document = vitals.get("document") or {}
    soft = bool(document) and urldefrag(document.get("url", ""))[0] != key
    audited[key] = {"desktop": {
        **checks,
        "first_seen_in": scenario,
        "ready": ready,
        "navigation": "client-side route change" if soft else "document load",
        "auth": {"login_form_visible": facts["login_form_visible"], "scenario_requires_auth": auth_required},
        # Document metrics belong to the URL that loaded the document; a soft route gets only its diagnostics.
        "document_metrics": None if soft else document,
        "route_diagnostics": vitals.get("route") if soft else None,
        "document_metrics_note": f"belong to the document load of {document.get('url')}" if soft else None,
    }}


def step_entry(h, screenshot):
    effect = h.get("effect") or {}
    return {
        "step": h["step"],
        "operation": h["operation"],
        "action": h["action"],
        "region": h.get("region"),
        "in_view": h.get("in_view"),
        "kind": h["kind"],
        "text": h["text"],
        "mutating": h.get("mutating"),
        "result": h.get("result"),
        "progress": h.get("progress"),
        "page_changed": h["page_changed"],
        "url": h["url"],
        "executed_ms": h["executed_ms"],
        "settle_ms": h["elapsed_ms"] - h["executed_ms"],
        "wait": {k: effect.get(k) for k in ("reason", "waited_ms", "mutations", "pending_requests", "navigated")},
        "jev_ms": h["latency_ms"],
        "text_ms": h["text_latency_ms"],
        "screenshot": screenshot,
    }


def run_scenario(spec, site, out, audited, defaults):
    name = spec.get("name") or spec["goal"][:40]
    folder = out / "scenarios" / slug(name)
    url = spec.get("url") or site
    reuse_tab = spec.get("reuse_tab", defaults["reuse_tab"])
    auth_required = bool(spec.get("requires_auth"))
    result = {"name": name, "goal": spec["goal"], "start_url": url, "expect": spec.get("expect") or {},
              "steps": [], "console": [], "pages": [], "reuse_tab": reuse_tab}
    max_steps = spec.get("max_steps", defaults["max_steps"])
    started = time.perf_counter()
    agent, events, observed = None, [], []
    upload_pages = set()  # steps after which the observed page asked for a file
    try:
        agent = Agent(url, spec["goal"], screenshots=True, init_script=CAPTURE, reuse_tab=reuse_tab,
                      max_seconds=spec.get("max_seconds", defaults["max_seconds"]), forbid=spec.get("forbid"),
                      scope=spec.get("scope"))
        browser = agent.browser
        baseline = safe_eval(browser, FACTS) or {"url": url, "title": "", "text": "", "login_form_visible": None}
        result["baseline"] = {k: baseline[k] for k in ("url", "title", "login_form_visible")}
        result["auth"] = {"required": auth_required, "login_form_visible_at_start": baseline["login_form_visible"],
                          "method": "visible password field in the page (no cookies or storage are read)"}
        result["steps"].append({"step": 0, "action": "open", "url": baseline["url"],
                                "screenshot": save_jpg(folder / "step-00.jpg", agent.state["page"]["screenshot"])})
        audit_page(browser, audited, name, auth_required)
        if agent.state["page"].get("file_inputs"):
            upload_pages.add(0)
        status = None
        if auth_required and baseline["login_form_visible"]:
            status = "not_run"  # Signed out: running the scenario would only test the login wall.
        else:
            for state in agent.run():
                events += [{**e, "after_step": len(state["history"])}
                           for e in (safe_eval(browser, "window.__ux?.drain()") or [])]
                observed.append(state["page"]["text"])
                if state["page"].get("file_inputs"):
                    upload_pages.add(len(state["history"]))
                for h in state["history"][len(result["steps"]) - 1:]:
                    shot = save_jpg(folder / f"step-{h['step']:02d}.jpg", state["page"]["screenshot"])
                    result["steps"].append(step_entry(h, shot))
                if state["status"] not in {"done", "blocked"}:
                    audit_page(browser, audited, name, auth_required)
                if len(state["history"]) >= max_steps and state["status"] not in {"done", "blocked"}:
                    status = "step_limit"
                    break
        state = agent.snapshot()
        drained = safe_eval(browser, "window.__ux?.drain()") or []
        events += [{**e, "after_step": len(state["history"])} for e in drained]
        result["status"] = status or state["status"]
        result["stop_reason"] = (status if status != "not_run" else "signed_out_at_start") or state.get("stop_reason")
        result["stop_evidence"] = state.get("stop_evidence", [])
        result["set_aside"] = state.get("guard", {}).get("excluded", {})
        result["refused_actions"] = state.get("guard", {}).get("refusals", [])
        result["decisions"] = [decision_summary(d) for d in state["decisions"]]
        result["usage"] = usage_totals(state)
        browser.ready(cap=3000)
        final = safe_eval(browser, FACTS) or {"url": state["page"]["url"], "title": state["page"]["title"],
                                              "text": "", "login_form_visible": None}
        result["final_url"], result["final_title"] = final["url"], final["title"]
        result["auth"]["login_form_visible_at_end"] = final["login_form_visible"]
        result["verification"] = evaluate_expectations(result["expect"], baseline, final, observed)
        # The executor never offers file inputs. An action without progress on a page that asks for a file is
        # the agent's limit, wherever it wandered afterwards.
        stuck = [s["step"] for s in result["steps"][1:] if s.get("progress") is False and s["step"] - 1 in upload_pages]
        if (final.get("file_inputs") or stuck) and not result["verification"]["all_passed"]:
            result["file_upload_pending"] = {"steps": stuck, "on_final_page": bool(final.get("file_inputs"))}
        result["final_metrics"] = safe_eval(browser, "window.__ux?.vitals()")
        result["final_screenshot"] = save_jpg(folder / "final.jpg", browser.call(
            "Page.captureScreenshot", format="jpeg", quality=72)["data"])
        result["final_full_page"] = full_page_screenshot(browser, folder / "final-full.jpg")
        result["left_state"] = {
            "tab": "user's own tab (left open on the final URL)" if not browser.owned else "tool tab (closed)",
            "tab_url_before": getattr(browser, "original_url", None),
            "tab_url_after": final["url"] if not browser.owned else None,
            "typed": [{"field": s["action"], "value": s["text"]} for s in result["steps"] if s.get("text")],
            "data_changing_actions": [{"step": s["step"], "action": s["action"], "result": s["result"]}
                                      for s in result["steps"] if s.get("mutating")],
            "note": "Drafts, filters or objects created server-side are not detected automatically; check the "
                    "data-changing actions and typed values above.",
        }
    except Exception as error:  # Keep auditing the other scenarios; the failure itself is evidence.
        result["status"] = "error"
        result["error"] = f"{type(error).__name__}: {error}"
        result["error_source"] = error_source(error)
        result["traceback"] = traceback.format_exc(limit=3)
        if agent:
            state = agent.snapshot()
            result["decisions"] = [decision_summary(d) for d in state["decisions"]]
            result["usage"] = usage_totals(state)
            for h in state["history"][len(result["steps"]) - 1:]:
                result["steps"].append(step_entry(h, None))
            try:
                result["final_screenshot"] = save_jpg(folder / "error.jpg", agent.browser.call(
                    "Page.captureScreenshot", format="jpeg", quality=72)["data"])
            except Exception:
                pass
    finally:
        if agent:
            agent.close()
    result["console"] = dedupe(events)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    result["outcome"], result["cause"], result["outcome_evidence"] = classify(result)
    result["verified"] = result["outcome"] == "passed"  # Schema 1 field, kept for compatibility.
    steps = [s for s in result["steps"] if s.get("step")]
    labels = [(s["action"], s.get("region")) for s in steps]
    result["signals"] = {
        "actions": len(steps),
        "decisions": len(result.get("decisions", [])),
        "no_effect_actions": sum(s.get("result") == "none" and s["kind"] != "wait" for s in steps),
        "no_progress_actions": sum(s.get("progress") is False and s["kind"] != "wait" for s in steps),
        "waits": sum(s["kind"] == "wait" for s in steps),
        "repeated_actions": sorted({f"{a} [{r}]" for a, r in labels if labels.count((a, r)) >= 3}),
        "ambiguous_decisions": sum(d["ambiguous"] for d in result.get("decisions", [])),
        "slowest_settle_ms": max((s["settle_ms"] for s in steps), default=0),
        "unique_errors": sum(e["kind"] != "console.warn" for e in result["console"]),
        "error_events": sum(e["count"] for e in result["console"] if e["kind"] != "console.warn"),
    }
    return result


def mobile_pass(urls, out, auth_required):
    """Fresh tabs at 390px. Per-tab state (sessionStorage sign-in, a cart) is NOT carried over: say so in reports."""
    pages = {}
    for i, url in enumerate(urls):
        browser = None
        try:
            browser = Browser(url, init_script=CAPTURE)
            browser.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
            browser.call("Emulation.setUserAgentOverride", userAgent=MOBILE_UA, platform="Android")
            browser.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
            browser.call("Page.reload", ignoreCache=False)
            time.sleep(0.3)
            ready = browser.ready(cap=4000)
            facts = safe_eval(browser, FACTS) or {}
            checks = safe_eval(browser, CHECKS) or {"error": "page did not settle"}
            vitals = checks.pop("vitals", None) or {}
            redirected = facts.get("url") if facts.get("url") and urldefrag(facts["url"])[0] != url else None
            checks.update({
                "ready": ready,
                "viewport_emulated": [390, 844],
                "navigation": "fresh tab, document load",
                "document_metrics": vitals.get("document"),
                "auth": {"login_form_visible": facts.get("login_form_visible"),
                         "scenario_requires_auth": auth_required,
                         "note": "fresh tab: per-tab sign-in and state are not inherited"},
                "redirected_to": redirected,
                "screenshot": save_jpg(out / "mobile" / f"{i:02d}-{slug(url)}.jpg", browser.call(
                    "Page.captureScreenshot", format="jpeg", quality=72)["data"]),
            })
            pages[url] = checks
        except Exception as error:
            pages[url] = {"error": f"{type(error).__name__}: {error}"}
        finally:
            if browser:
                browser.close()
    return pages


def issue_line(issues):
    return ", ".join(f"{k} {v['count']}" for k, v in sorted(issues.items(), key=lambda kv: -kv[1]["count"])) or "none"


def metric_line(m):
    if not m:
        return "unavailable"
    lcp = f"{m['lcp_ms']} ms" if m.get("lcp_ms") is not None else m.get("lcp_status", "unavailable")
    return (f"LCP {lcp} · CLS {m['cls']} (session window) · TTFB {m['ttfb_ms']} ms · load {m['load_ms']} ms · "
            f"long tasks {m['long_tasks']} · failed resources {len(m['failed_resources'])}")


def scenario_lines(s):
    lines = ["", f"### {s['name']}", "", f"Goal: {s['goal']}", "",
             f"**Outcome:** {s.get('outcome')} · cause: {s.get('cause') or '—'} · {s.get('outcome_evidence')}"]
    if s.get("error"):
        lines.append(f"**Error:** {s['error']}")
    for c in (s.get("verification") or {}).get("checks", []):
        lines.append(f"- check {c['check']}: start {'✓' if c['at_start'] else '✗'} → end {'✓' if c['at_end'] else '✗'}")
    lines.append("")
    for step in s["steps"][1:]:
        text = f" = {step['text']!r}" if step["text"] else ""
        where = f" [{step['region']}]" if step.get("region") else ""
        offscreen = " (was below the fold)" if step.get("in_view") is False else ""
        progress = "" if step.get("progress") is not False else " · no progress"
        mut = " · data-changing" if step.get("mutating") else ""
        wait = step.get("wait") or {}
        lines.append(f"{step['step']}. `{step['operation']}` {step['action'][:70]}{where}{offscreen}{text} → "
                     f"{step.get('result')} ({wait.get('reason')}, {step['settle_ms']} ms){progress}{mut}")
    if s.get("stop_reason"):
        evidence = json.dumps(s.get("stop_evidence"), ensure_ascii=False)[:600]
        lines.append(f"\nAgent stop: {s['stop_reason']}. Evidence: {evidence}")
    if s.get("set_aside"):
        lines.append("Set aside: " + "; ".join(f"{k.split('|', 2)[2]} [{k.split('|', 2)[1]}]: {v}"
                                               for k, v in s["set_aside"].items()))
    ambiguous = [d for d in s.get("decisions", []) if d["ambiguous"]]
    if ambiguous:
        lines += ["", "Ambiguous target choices (differently named or placed controls competed):"]
        for d in ambiguous[:8]:
            alts = "; ".join(f"{a['option'][:60]} {a['p']}" for a in d["target_alternatives"])
            lines.append(f"- step {d['step']}: {d['operation']} → {alts}")
    if s["signals"]["repeated_actions"]:
        lines.append(f"\nRepeated ≥3×: {', '.join(s['signals']['repeated_actions'])}")
    errors = [e for e in s["console"] if e["kind"] != "console.warn"]
    if errors:
        lines += ["", "Console/runtime errors (deduplicated):"]
        lines += [f"- {e.get('count', 1)}× [{e['kind']}] {e['message'][:160]}" for e in errors[:8]]
    metrics = (s.get("final_metrics") or {}).get("document")
    if metrics:
        lines.append(f"\nFinal document ({metrics['url']}): {metric_line(metrics)}")
    if s.get("left_state"):
        left = s["left_state"]
        actions = [a["action"] for a in left["data_changing_actions"]] or "none"
        lines.append(f"\nLeft behind: {left['tab']}; typed {len(left['typed'])} value(s); "
                     f"data-changing actions: {actions}")
    if s.get("usage"):
        u = s["usage"]
        cost = f"${u['gateway_cost_usd']}" if u["gateway_cost_usd"] is not None else "not reported"
        lines.append(f"Model use: {u['decision_calls']} × {u['decision_model']} "
                     f"({u['decision_input_tokens']} in / {u['decision_output_tokens']} out tokens), "
                     f"{u['text_calls']} × {u['text_model']}; gateway cost {cost}")
    return lines


def page_lines(url, page):
    lines = [f"### {url}", ""]
    for mode in ("desktop", "mobile"):
        p = page.get(mode)
        if not p:
            continue
        if "error" in p:
            lines.append(f"- {mode}: {p['error']}")
            continue
        auth = p.get("auth") or {}
        redirect = f" (redirected to {p['redirected_to']})" if p.get("redirected_to") else ""
        lines.append(f"- {mode} {p['viewport'][0]}px, {p.get('navigation')}, ready: "
                     f"{(p.get('ready') or {}).get('reason')}, login form visible: "
                     f"{auth.get('login_form_visible')}{redirect}")
        lines.append(f"  - issues: {issue_line(p['issues'])}")
        if p["structure"]:
            lines.append(f"  - structure: {'; '.join(p['structure'])}")
        if p.get("document_metrics"):
            lines.append(f"  - document metrics: {metric_line(p['document_metrics'])}")
        elif p.get("route_diagnostics"):
            r = p["route_diagnostics"]
            lines.append(f"  - route diagnostics (not CWV): layout shift since route change {r['layout_shift_sum']}, "
                         f"long tasks {r['long_tasks']}; document metrics {p['document_metrics_note']}")
    return lines


def summarize(report):
    lines = [f"# UX audit evidence: {report['site']}", "",
             f"Run at {report['started']}, {report['elapsed_s']} s total. Schema {report.get('schema_version', 1)}. "
             "Metrics are one lab run in the user's Chrome, not field Core Web Vitals.", ""]
    lines += ["## Scenarios", "",
              "| Scenario | Outcome | Cause | Agent stop | Actions | No progress | Ambiguous | Errors | Time |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in report["scenarios"]:
        sig = s["signals"]
        lines.append(f"| {s['name']} | {s.get('outcome')} | {s.get('cause') or '—'} | "
                     f"{s.get('stop_reason') or s.get('status')} | {sig['actions']} | "
                     f"{sig.get('no_progress_actions', 0)} | {sig['ambiguous_decisions']} | "
                     f"{sig.get('unique_errors', 0)} | {s['elapsed_ms'] / 1000:.1f} s |")
    for s in report["scenarios"]:
        lines += scenario_lines(s)
    lines += ["", "## Pages", ""]
    for url, page in report["pages"].items():
        lines += page_lines(url, page)
    lines += ["", "Full detail with element examples: results.json. Screenshots: scenarios/*/ and mobile/."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="?", help="scenarios.json")
    parser.add_argument("--url")
    parser.add_argument("--goal", action="append", default=[])
    parser.add_argument("--out", required=True)
    parser.add_argument("--no-mobile", action="store_true")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-seconds", type=int, default=180)
    parser.add_argument("--max-mobile-pages", type=int, default=6)
    parser.add_argument("--reuse-tab", action="store_true",
                        help="drive the user's open tab for this site instead of a new one (keeps per-tab sign-in)")
    args = parser.parse_args()

    if args.scenarios:
        config = json.loads(Path(args.scenarios).read_text())
    elif args.url and args.goal:
        config = {"site": args.url, "scenarios": [{"name": g[:40], "goal": g} for g in args.goal]}
    else:
        parser.error("pass scenarios.json, or --url with at least one --goal")
    site = config.get("site") or config["scenarios"][0].get("url")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    defaults = {"reuse_tab": args.reuse_tab or config.get("reuse_tab", False), "max_steps": args.max_steps,
                "max_seconds": args.max_seconds}
    started = time.perf_counter()
    audited = {}
    report = {"schema_version": SCHEMA_VERSION, "site": site, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
              "scenarios": []}
    for spec in config["scenarios"]:
        print(f"▶ {spec.get('name') or spec['goal'][:40]}", flush=True)
        result = run_scenario(spec, site, out, audited, defaults)
        sig = result["signals"]
        stop = result.get("stop_reason") or result["status"]
        print(f"  {result['outcome']} ({result['cause'] or '—'}) · agent {stop}"
              f" · {sig['actions']} actions · {sig['no_progress_actions']} without progress · "
              f"{result['elapsed_ms'] / 1000:.1f} s", flush=True)
        report["scenarios"].append(result)
    if config.get("mobile", True) and not args.no_mobile:
        urls = list(dict.fromkeys([site, *audited]))[: args.max_mobile_pages]
        print(f"▶ mobile pass on {len(urls)} pages", flush=True)
        auth_required = any(s.get("requires_auth") for s in config["scenarios"])
        for url, checks in mobile_pass(urls, out, auth_required).items():
            audited.setdefault(url, {})["mobile"] = checks
    report["pages"] = audited
    report["elapsed_s"] = round(time.perf_counter() - started, 1)
    (out / "results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (out / "summary.md").write_text(summarize(report))
    print(f"✓ {out / 'summary.md'}")
    return 0 if all(s["status"] != "error" for s in report["scenarios"]) else 1


if __name__ == "__main__":
    sys.exit(main())

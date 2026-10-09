"""Real-Chrome regressions for the ux-audit skill and the agent loop. No model calls, no external websites.

    uv run python scripts/check_ux_regressions.py
    # Same checks against another copy of the code (e.g. the version before a fix):
    PYTHONPATH=/path/to/old uv run python scripts/check_ux_regressions.py --skill-dir /path/to/old/skills/ux-audit

A scripted, deliberately stubborn policy replaces Jev, so outcomes depend on the loop's code, not on model
choices. Each check asserts behaviour (server-side counters, what the browser shows), not implementation details.
"""

import argparse
import importlib.util
import inspect
import json
import sys
import tempfile
import threading
import time
import traceback
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from browser_harness.helpers import cdp

import jev_ultrafast
from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import Browser

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "tests" / "fixtures" / "ux_site"
COUNTS = {"cart": 0, "submit": 0}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path.startswith("/spa"):
            self.path = "/spa.html"
        return super().do_GET()

    def do_POST(self):
        name = self.path.rsplit("/", 1)[-1]
        COUNTS[name] = COUNTS.get(name, 0) + 1
        time.sleep({"cart": 0.6, "submit": 7.0}.get(name, 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(SITE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def supported(fn, **kwargs):
    accepted = inspect.signature(fn).parameters
    return {k: v for k, v in kwargs.items() if k in accepted}


def scripted(pick):
    """A stand-in for Jev. pick(page, history, offered) returns an action id, 'DONE', 'BLOCKED' or None (wait)."""
    def choose(page, goal, history, excluded=None, **_):
        offered = page["actions"]
        if excluded:  # Same filtering the real policy gets: set-aside controls are not offered.
            offered = [a for a in offered if model.action_key(a) not in excluded]
        choice = pick(page, history, offered)
        if choice is None:
            choice = next(a["id"] for a in offered if a["kind"] == "wait")
        operation = choice if choice in {"DONE", "BLOCKED"} else "CLICK"
        return {"choice": choice, "operation": operation, "target": None, "confidence": 1.0,
                "probabilities": {choice: 1.0}, "operation_probabilities": {operation: 1.0},
                "target_probabilities": {}, "raw_answers": {}, "model": "scripted", "usage": {},
                "latency_ms": 0, "request": {"questions": {}}}
    return choose


def by_label(offered, text):
    return next((a["id"] for a in offered if a["kind"] == "click" and text in a["label"]), None)


def run_agent(url, pick, max_ticks=14, **kwargs):
    loop.choose = scripted(pick)
    agent = loop.Agent(url, "fixture goal", **supported(loop.Agent.__init__, **kwargs))
    try:
        for i, state in enumerate(agent.run()):
            if i + 1 >= max_ticks:
                break
        state = agent.snapshot()
        state["dom_text"] = agent.browser.evaluate("document.body.innerText")  # page["text"] is viewport-only
        return state
    finally:
        agent.close()


def load_skill(skill_dir):
    spec = importlib.util.spec_from_file_location("ux_audit_under_test", Path(skill_dir) / "scripts" / "ux_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def audit(ux, url, pick, expect=None, **spec_extra):
    loop.choose = scripted(pick)
    ux.Agent = loop.Agent
    out = Path(tempfile.mkdtemp(prefix="ux-regression-"))
    spec = {"name": "fixture", "goal": "fixture goal", "url": url, "expect": expect or {}, **spec_extra}
    audited = {}
    params = inspect.signature(ux.run_scenario).parameters
    if "defaults" in params:
        result = ux.run_scenario(spec, url, out, audited, {"reuse_tab": False, "max_steps": 12, "max_seconds": 60})
    else:
        result = ux.run_scenario(spec, url, out, audited, 12)
    return result, audited


# 1. Delayed SPA render: the agent and the page checks must see the rendered route, not the loading shell.
def check_delayed_render(base, ux):
    seen = {}

    def pick(page, history, offered):
        seen.setdefault("first", [a["label"] for a in page["actions"]])
        return "DONE"

    result, audited = audit(ux, base + "/spa/a", pick)
    page = audited.get(base + "/spa/a", {}).get("desktop", {})
    assert any("Go to route B" in label for label in seen["first"]), f"first observation: {seen['first']}"
    assert "no visible h1" not in page.get("structure", []), f"page checks ran on the shell: {page.get('structure')}"


# 2. Same accessible name in header search and in the form: the observation must tell them apart.
def check_same_names_have_regions(base, _ux):
    browser = Browser(base + "/header-form.html")
    try:
        page = browser.observe(screenshot=False)
    finally:
        browser.close()
    category = [a for a in page["actions"] if a["label"].startswith("Category")]
    regions = {a.get("region") for a in category}
    assert len(category) == 2, category
    pairs = [(a["label"], a.get("region")) for a in category]
    assert None not in regions and len(regions) == 2, f"indistinguishable: {pairs}"
    elements = model.action_space(page["actions"])[0]
    assert len({e.get("region") for e in elements if e["label"].startswith("Category")}) == 2, "regions not sent to Jev"


# 3. Targets below the viewport and inside a scroll container are reachable.
def check_offscreen_and_container(base, _ux):
    state = run_agent(base + "/long.html", lambda page, history, offered: (
        by_label(offered, "Load all reviews") if not history else "DONE"), max_ticks=3)
    assert "All reviews loaded." in state["dom_text"], "offscreen button was not offered or not clicked"
    state = run_agent(base + "/long.html", lambda page, history, offered: (
        "DONE" if "Colour 30" in page["text"] and len(history) > 0 and history[-1]["action"].startswith("Colour 30")
        else by_label(offered, "Colour 30") or next((a["id"] for a in offered if a["kind"] == "scroll"
                                                     and "inside" in a["label"] and a["delta"] > 0), None)),
        max_ticks=10)
    assert "Chosen colour 30." in state["dom_text"], "item at the end of the scroll container was not reached"
    # The container's own scroll action must move that container, not the page.
    browser = Browser(base + "/long.html")
    try:
        page = browser.observe(screenshot=False)
        scroll = next((x for x in page["actions"] if x["kind"] == "scroll" and "inside" in x["label"]
                       and x["delta"] > 0), None)
        assert scroll, f"no container scroll offered: {[x['label'] for x in page['actions'] if x['kind'] == 'scroll']}"
        browser.act(scroll, page)
        moved = browser.evaluate("[document.getElementById('list').scrollTop, scrollY]")
        assert moved[0] > 0 and moved[1] == 0, f"container/page scrollTop after container scroll: {moved}"
    finally:
        browser.close()


# 4. Slow add-to-cart: a stubborn policy keeps choosing it; the server must see exactly one add.
def check_single_add_to_cart(base, _ux):
    COUNTS["cart"] = 0
    state = run_agent(base + "/cart.html", lambda page, history, offered: by_label(offered, "Add to cart"), max_ticks=6)
    assert COUNTS["cart"] == 1, f"server received {COUNTS['cart']} add-to-cart requests"
    assert state["history"][0].get("result") in {"changed", "transient"}, state["history"][0]


# 5. Submit whose response outlives the wait, then succeeds: never submit twice.
def check_no_double_submit(base, ux):
    COUNTS["submit"] = 0

    def pick(page, history, offered):
        if "Thanks, your message was received." in page["text"]:
            return "DONE"
        return by_label(offered, "Send message")  # Stubborn: when it is set aside, this falls back to WAIT.

    result, _ = audit(ux, base + "/submit.html", pick,
                      expect={"text_contains": ["Thanks, your message was received."]}, max_seconds=40)
    time.sleep(1)
    assert COUNTS["submit"] == 1, f"server received {COUNTS['submit']} submissions"
    assert result.get("outcome", "passed" if result.get("verified") else None) == "passed", result.get("outcome")


# 6. Expected text already on the start page is not proof that the scenario did anything.
def check_already_present_text(base, ux):
    result, _ = audit(ux, base + "/already.html", lambda *_: "DONE", expect={"text_contains": ["Order confirmed"]})
    assert result.get("verified") is not True, "verified from text that was present before any action"
    assert result.get("outcome") == "inconclusive", result.get("outcome")


# 7. Client-side routes must not repeat the document's metrics as if each were measured independently.
def check_spa_metrics(base, ux):
    def pick(page, history, offered):
        if "Second route content" in page["text"]:
            return "DONE"
        return by_label(offered, "Go to route B")

    result, audited = audit(ux, base + "/spa/a", pick)
    a, b = (audited.get(base + p, {}).get("desktop", {}) for p in ("/spa/a", "/spa/b"))
    assert a and b, f"routes audited: {list(audited)}"
    if "document_metrics" not in b:  # Older schema: one "vitals" copy per page.
        raise AssertionError(f"both routes report the same document vitals: {a.get('vitals') == b.get('vitals')}")
    assert a["document_metrics"] and not b["document_metrics"], "route B claims document-load metrics"
    route = (result.get("final_metrics") or {}).get("route") or {}
    assert route.get("url", "").endswith("/spa/b") and route.get("layout_shift_sum", 0) > 0, \
        f"route B's own layout shift was not attributed to it: {route}"


# 8. Per-tab sign-in: a new tab is signed out, the user's tab is signed in; missing auth is environment.
def check_per_tab_session(base, ux):
    target = cdp("Target.createTarget", url=base + "/account.html", background=True)["targetId"]
    session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
    try:
        time.sleep(0.8)
        cdp("Runtime.evaluate", session_id=session,
            expression="sessionStorage.setItem('fixture-user','test'); location.reload()")
        time.sleep(0.8)
        fresh = Browser(base + "/account.html")
        try:
            assert "Sign in" in fresh.evaluate("document.body.innerText"), "new tab unexpectedly signed in"
        finally:
            fresh.close()
        own = Browser(base + "/account.html", **supported(Browser.__init__, reuse_tab=True))
        try:
            assert "Signed in as Test User" in own.evaluate("document.body.innerText"), "reuse-tab lost the session"
        finally:
            own.close()
        assert any(t["targetId"] == target for t in cdp("Target.getTargets")["targetInfos"]), "user tab was closed"
    finally:
        cdp("Target.detachFromTarget", sessionId=session)
        cdp("Target.closeTarget", targetId=target)
    result, _ = audit(ux, base + "/account.html", lambda *_: "DONE", expect={"text_contains": ["Signed in as"]},
                      requires_auth=True)
    assert (result.get("outcome"), result.get("cause")) == ("blocked", "environment"), \
        (result.get("outcome"), result.get("cause"), result.get("status"))


# 9. A loop without progress ends with evidence, long before the step budget.
def check_loop_stops(base, _ux):
    state = run_agent(base + "/loop.html", lambda page, history, offered: by_label(offered, "Filters"), max_ticks=12)
    assert state["status"] == "blocked" and state.get("stop_reason") == "no_progress_loop", \
        (state["status"], state.get("stop_reason"), len(state["history"]))
    assert len(state["history"]) <= 5, f"{len(state['history'])} actions before stopping"


# 10. Cleanup after an exception and after a timeout leaves the user's tab open and free of tool hooks.
def check_cleanup(base, ux):
    target = cdp("Target.createTarget", url=base + "/cart.html", background=True)["targetId"]
    session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]

    def probe():
        expression = ("JSON.stringify({ux: typeof window.__ux, settle: typeof window.__jevSettle, "
                      "fast: typeof window.__jevFast, fetch: fetch.toString().includes('[native code]'), "
                      "console: console.error.toString().includes('[native code]'), "
                      "push: history.pushState.toString().includes('[native code]'), width: innerWidth})")
        return json.loads(cdp("Runtime.evaluate", session_id=session, expression=expression,
                              returnByValue=True)["result"]["value"])

    try:
        time.sleep(0.8)
        width = probe()["width"]
        clean = {"ux": "undefined", "settle": "undefined", "fast": "undefined", "fetch": True, "console": True,
                 "push": True, "width": width}
        browser = Browser(base + "/cart.html", **supported(Browser.__init__, reuse_tab=True, init_script=ux.CAPTURE))
        try:
            browser.observe(screenshot=False)
            browser.act(next(a for a in browser.observe(screenshot=False)["actions"] if a["label"] == "Add to cart"),
                        browser.observe(screenshot=False))
            raise RuntimeError("simulated failure mid-run")
        except RuntimeError:
            pass
        finally:
            browser.close()
        after_exception = probe()
        assert after_exception == clean, f"after exception: {after_exception}"
        cdp("Runtime.evaluate", session_id=session, expression="location.reload()")
        time.sleep(0.8)
        assert probe()["ux"] == "undefined", "init script still injected after reload"
        loop.choose = scripted(lambda *_: None)
        agent = loop.Agent(base + "/cart.html", "goal", **supported(loop.Agent.__init__, reuse_tab=True,
                                                                     init_script=ux.CAPTURE, max_seconds=0.5))
        try:
            for i, _ in enumerate(agent.run()):
                if i > 20:
                    break
        finally:
            agent.close()
        assert agent.state.get("stop_reason") == "time_budget", agent.state.get("stop_reason")
        assert probe() == clean, f"after timeout: {probe()}"
        assert any(t["targetId"] == target for t in cdp("Target.getTargets")["targetInfos"]), "user tab was closed"
    finally:
        cdp("Target.detachFromTarget", sessionId=session)
        cdp("Target.closeTarget", targetId=target)


# 3b. A control under a sticky header is scrolled clear and used; one under a real overlay is refused.
def check_sticky_header_and_overlay(base, _ux):
    browser = Browser(base + "/select-above.html")
    try:
        # Put the select's centre under the 110 px sticky header, still inside the viewport.
        browser.evaluate("scrollTo(0, document.getElementById('floor').getBoundingClientRect().top + scrollY - 60)")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["kind"] == "select" and a["value"] == "bajo")
        browser.act(action, page)
        assert browser.evaluate("document.getElementById('floor').value") == "bajo", "select under the header failed"
        browser.evaluate("document.getElementById('modal').hidden=false")
        page = browser.observe(screenshot=False)
        action = next((a for a in page["actions"] if a["kind"] == "select" and a["value"] == "1"), None)
        if action:
            try:
                browser.act(action, page)
                raise AssertionError("acted through a full-page overlay")
            except RuntimeError as refused:
                assert "covered" in str(refused), refused
        assert browser.evaluate("document.getElementById('floor').value") == "bajo", "value changed under an overlay"
    finally:
        browser.close()


# 11. A required upload the agent cannot perform is classified as an agent limit, even after it wandered off.
def check_upload_wall_is_agent_limit(base, ux):
    def pick(page, history, offered):
        if len(history) < 3:
            return by_label(offered, "Continue")  # Set aside after two attempts without progress.
        if "Your orders" in page["text"]:
            return "BLOCKED"
        return by_label(offered, "Back to orders")

    result, _ = audit(ux, base + "/upload.html", pick, expect={"text_contains": ["Review"]})
    assert (result.get("outcome"), result.get("cause")) == ("blocked", "agent"), \
        (result.get("outcome"), result.get("cause"), result.get("outcome_evidence"))


CHECKS = [check_delayed_render, check_same_names_have_regions, check_offscreen_and_container,
          check_sticky_header_and_overlay,
          check_single_add_to_cart, check_no_double_submit, check_already_present_text, check_spa_metrics,
          check_per_tab_session, check_loop_stops, check_cleanup, check_upload_wall_is_agent_limit]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", default=str(ROOT / "skills" / "ux-audit"))
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    ux = load_skill(args.skill_dir)
    server, base = serve()
    print(f"code under test: {Path(jev_ultrafast.__file__).parent}, skill: {args.skill_dir}")
    failures = 0
    for check in CHECKS:
        if args.only and not any(o in check.__name__ for o in args.only):
            continue
        started = time.perf_counter()
        try:
            check(base, ux)
            print(f"PASS {check.__name__} ({time.perf_counter() - started:.1f} s)", flush=True)
        except Exception as error:
            failures += 1
            detail = str(error) or traceback.format_exc(limit=2).strip().splitlines()[-1]
            print(f"FAIL {check.__name__}: {type(error).__name__}: {detail[:300]}", flush=True)
    server.shutdown()
    print(f"{len(CHECKS) - failures if not args.only else '?'} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

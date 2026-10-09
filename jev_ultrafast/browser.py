"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"
SETTLE = Path(__file__).with_name("settle.js").read_text().strip().rstrip(";")
TEARDOWN = "window.__jevSettle?.dispose(); window.__ux?.teardown?.(); delete window.__jevFast; true"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


def site_tab(url):
    """An open tab on the same site as url: it carries the profile (and tab) where the user is signed in."""
    host = urlsplit(url).hostname or ""
    site = host.removeprefix("www.")
    for target in cdp("Target.getTargets")["targetInfos"]:
        tab_host = (urlsplit(target["url"]).hostname or "").removeprefix("www.")
        if target["type"] == "page" and site and tab_host == site:
            return target
    return None


class Browser:
    def __init__(self, url, init_script=None, reuse_tab=False):
        ensure_daemon()
        tab = site_tab(url)
        self.owned = not (reuse_tab and tab)
        if self.owned:
            # Open next to the user's tab for this site, so a non-default Chrome profile keeps its sign-in.
            contexts = cdp("Target.getBrowserContexts").get("browserContextIds", [])
            context = tab.get("browserContextId") if tab else None
            extra = {"browserContextId": context} if context in contexts else {}
            self.target = cdp("Target.createTarget", url="about:blank", background=True, **extra)["targetId"]
        else:
            # Drive the user's own tab: per-tab state such as sessionStorage sign-ins stays available.
            self.target = tab["targetId"]
            self.original_url = tab["url"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        if init_script:
            # Runs before page scripts in every document of this tab, e.g. to record console errors.
            self.call("Page.enable")
            self.init_script = self.call("Page.addScriptToEvaluateOnNewDocument", source=init_script)["identifier"]
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def settle(self, action=None, previous_document=None):
        """Wait for the effect of the last input, bounded; report what happened instead of assuming it."""
        kind = (action or {}).get("kind")
        options = {"quiet": 250, "noEffect": 1000, "cap": 3000}
        if kind == "fill":
            options = {"quiet": 150, "noEffect": 300, "cap": 1200}
            if action.get("role") == "combobox":
                options["autocompleteNode"] = action["node"]
        elif kind == "scroll":
            options = {"quiet": 120, "noEffect": 200, "cap": 800}
        elif kind == "wait":
            options = {"quiet": 300, "noEffect": 1000, "cap": 2000}
        elif (action or {}).get("mutating"):
            options = {"quiet": 300, "noEffect": 1500, "cap": 5000}
        deadline = time.monotonic() + options["cap"] / 1000 + 10
        while time.monotonic() < deadline:
            try:
                response = self.call(
                    "Runtime.evaluate",
                    expression=f"document.readyState === 'complete' ? ({SETTLE}, "
                    f"window.__jevSettle.wait({json.dumps(options)}).then(r => ({{...r, "
                    "document_id: performance.timeOrigin}))) : null",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except (RuntimeError, TimeoutError):
                response = {}
            result = response.get("result", {}).get("value") if not response.get("exceptionDetails") else None
            if result and result.get("reason") == "continue":
                continue  # Still within the cap; the page keeps the armed start time.
            if result is None:
                time.sleep(0.05)  # Navigating: the old document is gone and the new one is not ready yet.
                continue
            if previous_document is not None and result["document_id"] != previous_document:
                # A new document loaded; give its client-side rendering the same bounded chance to finish.
                ready = self.ready(cap=options["cap"])
                return {**ready, "reason": ready["reason"], "navigated": True}
            return {**result, "navigated": False}
        return {"reason": "timeout", "navigated": None, "waited_ms": None}

    def ready(self, cap=4000):
        """Bounded wait until the current document has loaded and its DOM stopped changing (SPA render)."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except (StalePage, RuntimeError):
                pass
            time.sleep(0.05)
        expression = f"({SETTLE}, window.__jevSettle.arm(), true)"
        for _ in range(cap // 2000 + 3):
            try:
                options = json.dumps({"quiet": 400, "noEffect": 600, "cap": cap})
                result = self.evaluate_async(f"({expression}, window.__jevSettle.wait({options}))")
            except (StalePage, RuntimeError, TimeoutError):
                return {"reason": "unavailable"}
            if not result or result.get("reason") != "continue":
                return result or {"reason": "unavailable"}
            expression = "true"  # Keep the armed start time across chunks.
        return {"reason": "timeout"}

    def evaluate_async(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            (action, document_id), self.after_input = self.after_input, None
            # Read-only, after execution was logged. Its result tells the agent whether the action did anything.
            self.last_effect = self.settle(action, document_id)
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = (action, page.get("document_id"))
        return result

    def close(self):
        if not self.target:
            return
        try:
            # Remove what this tool put into the page: request counters, observers, console hooks, node cache.
            self.call("Runtime.evaluate", expression=TEARDOWN, returnByValue=True)
        except RuntimeError:
            pass
        if self.owned:
            cdp("Target.closeTarget", targetId=self.target)
        else:
            # The user's tab stays open on whatever page the run ended; only emulation and scripts are undone.
            for method, params in (
                ("Emulation.clearDeviceMetricsOverride", {}),
                ("Emulation.setFocusEmulationEnabled", {"enabled": False}),
                ("Page.removeScriptToEvaluateOnNewDocument", {"identifier": getattr(self, "init_script", None)}),
            ):
                if params.get("identifier", True) is None:
                    continue
                try:
                    self.call(method, **params)
                except RuntimeError:
                    pass
            cdp("Target.detachFromTarget", sessionId=self.session)
        self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        # Arm the effect detector right before input, so the wait measures this action only.
        arm = f"({SETTLE}, window.__jevSettle.arm())"
        if kind == "wait":
            evaluate(arm)
        elif kind == "scroll":
            # The page or an observed scroll container; a node ID from the snapshot, never a model-made selector.
            node = action.get("node")
            if node is not None and type(node) is not int:
                raise ValueError("Invalid observed node")
            moved = evaluate(arm + "; (action => { const c=action.node==null ? null : "
                             "window.__jevFast?.nodes.get(action.node); if (action.node!=null && !c?.isConnected) "
                             "return false; const t=c||document.scrollingElement, before=t.scrollTop; "
                             "t.scrollBy({top:action.delta,behavior:'instant'}); return t.scrollTop!==before; })("
                             + json.dumps(action) + ")")
            if moved is False:
                raise StalePage("Scroll container is gone. Observe again.")
        else:
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate(arm + """; (action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected) return {fail:'target is gone'};
              if (e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]'))
                return {fail:'target is disabled'};
              if (!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return {fail:'target is hidden'};
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true'))
                return {fail:'field is read-only'};
              let r=e.getBoundingClientRect();
              if (r.y+r.height/2<0 || r.y+r.height/2>=innerHeight) {
                // Offscreen targets are real page content: bring into view, then hit-test current geometry.
                e.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
                r=e.getBoundingClientRect();
              }
              let x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight)
                return {fail:'target is outside the viewport'};
              let hit=document.elementFromPoint(x,y);
              if (!e.contains(hit)) {
                // Often a sticky header or footer over a control at the edge: a person would scroll. Scroll once;
                // a real overlay (modal, cookie wall) still covers it afterwards and the click is refused.
                e.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
                r=e.getBoundingClientRect(); x=r.x+r.width/2; y=r.y+r.height/2;
                hit=document.elementFromPoint(x,y);
              }
              if (!e.contains(hit)) return {fail:'target is covered by '+(hit?.tagName||'nothing').toLowerCase()+
                (hit?.id ? '#'+hit.id : '')};
              if (action.kind==='select') {
                if (e.tagName!=='SELECT') return {fail:'target is no longer a native select'};
                if (![...e.options].some(o=>o.value===action.value && !o.disabled && !o.closest('optgroup[disabled]')))
                  return {fail:'option '+JSON.stringify(action.value)+' is no longer offered'};
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None or "fail" in target:
                reason = (target or {}).get("fail", "no result")
                if kind == "select":
                    raise RuntimeError(f"Dropdown execution was not confirmed ({reason}); inspect before retrying.")
                raise StalePage(f"Target changed or is covered ({reason}). Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info

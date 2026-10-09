"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .browser import Browser, StalePage
from .model import action_key, action_space, choose, field_context, field_text
from .questions import MAX_STEPS

# Clicks that likely change data on the server. A heuristic: a missed match falls back to the loop rules,
# a false match only means the control runs at most once without changed inputs.
MUTATING = re.compile(
    r"\b(submit|send|publish|post (ad|listing)|delete|remove|add to (cart|bag|basket)|buy|purchase|pay|"
    r"place order|order now|checkout|confirm|save|subscribe|sign up|register|book now|reserve|"
    r"enviar|publicar|eliminar|borrar|añadir|comprar|pagar|confirmar|guardar|suscrib\w*|registr\w*|reservar|"
    r"надіслати|відправити|опублікувати|видалити|купити|оплатити|замовити|підтвердити|зберегти|"
    r"додати (в|до) кошик\w*|отправить|опубликовать|удалить|купить|оплатить|заказать|подтвердить|сохранить)\b",
    re.IGNORECASE,
)
CONFIRMED = {"navigated", "changed"}
NO_PROGRESS_REPEATS = 2  # identical attempts without progress before the target is set aside
NO_PROGRESS_STREAK = 6  # consecutive actions without a new state, whatever they were
WAIT_STREAK = 8


def is_mutating(action):
    return action["kind"] == "click" and bool(action.get("submit") or MUTATING.search(action.get("label", "")))


def state_key(page):
    """Meaning of the page, without geometry: returning to a previous state is not progress."""
    return hashlib.sha256(json.dumps(page.get("marker", page["fingerprint"]), sort_keys=True).encode()).hexdigest()


def inputs(page):
    key = page.get("page_key")
    return key[6] if isinstance(key, list) and len(key) > 6 else None


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False, init_script=None, reuse_tab=False,
                 max_seconds=180, forbid=None, scope=None):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        self.browser = Browser(url, init_script=init_script, reuse_tab=reuse_tab)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            self.browser.ready()  # A client-rendered page may still be an empty shell at document load.
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            max_seconds=max_seconds,
            forbid=forbid,
            scope=scope,
            stop_reason=None,
            stop_evidence=[],
            guard=new_guard(page),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                if state["status"] in {"done", "blocked"}:  # A budget stopped the run before any choice.
                    return self.snapshot()
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= MAX_STEPS * 2:
                raise ValueError("Reached the demo's model-call budget")
            elapsed = time.perf_counter() - state["started_at"]
            if elapsed > state.get("max_seconds", 180):
                return self.stop("time_budget", [f"{elapsed:.0f} s elapsed, budget {state['max_seconds']} s"])
            guard = state.setdefault("guard", new_guard(state["page"]))
            for a in state["page"]["actions"]:
                if self.forbidden(a):
                    guard["excluded"][action_key(a)] = "forbidden by the scenario"
                elif self.out_of_scope(a):
                    guard["excluded"][action_key(a)] = "outside the scenario's scope"
            state["decision"] = choose(state["page"], state["goal"], state["history"],
                                       excluded=guard["excluded"])
            state["decisions"].append(
                {
                    **state["decision"],
                    "step": len(state["history"]) + 1,
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            guard = state.setdefault("guard", new_guard(page))
            if self.forbidden(action) or self.out_of_scope(action):  # Defense in depth: never execute.
                evidence = [{"action": action["label"], "region": action.get("region")}]
                return self.stop("forbidden_action_chosen", evidence)
            mutating = is_mutating(action)
            mutation = f"{action_key(action)}|{urlsplit(page['url']).path}"
            previous = guard["mutations"].get(mutation)
            if previous and not (previous["result"] in CONFIRMED and previous["inputs"] != inputs(page)):
                # Never repeat a data-changing click whose earlier effect is unknown, or that would run again
                # on unchanged inputs (double add-to-cart, double submit). Set it aside and let the model rethink.
                guard["excluded"][action_key(action)] = "refused: repeat of a data-changing action"
                guard["refusals"].append({"action": action["label"], "region": action.get("region"),
                                          "previous_result": previous["result"]})
                if len(guard["refusals"]) >= 2:
                    return self.stop("repeat_mutation_refused", guard["refusals"])
                state["status"] = "ready"
                return self.snapshot()
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Browser.act checks freshness immediately before input, including after text generation.
            state["browser"].act({**action, "mutating": mutating}, page, text=text)
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "region": action.get("region"),
                    "in_view": action.get("in_view"),
                    "mutating": mutating,
                    "page_changed": None,
                    "result": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            effect = getattr(state["browser"], "last_effect", None)
            effect = effect if isinstance(effect, dict) else {}
            changed = state["page"]["fingerprint"] != page["fingerprint"]
            if effect.get("navigated") or state["page"]["url"] != page["url"]:
                result = "navigated"
            elif changed:
                result = "changed"
            elif effect.get("reason") == "timeout":
                result = "timeout"  # Still busy when the wait ended: the outcome is unknown, not absent.
            elif effect.get("mutations"):
                result = "transient"  # Something appeared and went away again (e.g. a toast).
            else:
                result = "none"
            key = state_key(state["page"])
            progress = key not in guard["seen"]
            guard["seen"][key] = guard["seen"].get(key, 0) + 1
            state["history"][-1].update(
                page_changed=changed,
                result=result,
                progress=progress,
                effect=effect,
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if mutating:
                guard["mutations"][mutation] = {"result": result, "inputs": inputs(page),
                                                "count": (previous or {}).get("count", 0) + 1}
                if result not in CONFIRMED | {"transient"}:
                    guard["excluded"][action_key(action)] = f"data-changing action with {result} effect"
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            state["status"] = "ready"
            self.track_progress(action, progress)
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def forbidden(self, action):
        pattern = self.state.get("forbid")
        return bool(pattern) and action["kind"] in {"click", "select"} and \
            re.search(pattern, action.get("label", ""), re.IGNORECASE) is not None

    def out_of_scope(self, action):
        """A scenario may confine targets to regions (e.g. 'form'), so a header search is never mistaken for it."""
        pattern = self.state.get("scope")
        return bool(pattern) and action["kind"] in {"click", "fill", "select"} and \
            re.search(pattern, action.get("region", ""), re.IGNORECASE) is None

    def track_progress(self, action, progress):
        """Stop loops by evidence: same target without progress, then any further failed attempt."""
        guard, history = self.state["guard"], self.state["history"]
        if action["kind"] == "wait":
            guard["wait_streak"] = 0 if progress else guard["wait_streak"] + 1
            if guard["rethink"] and not progress:
                # After a target was set aside, waiting without any change is the next failed attempt.
                self.stop("no_progress_loop", [{k: h.get(k) for k in ("step", "action", "region", "result", "url")}
                                               for h in history[-4:]])
            elif guard["wait_streak"] >= WAIT_STREAK:
                self.stop("waiting_without_change", history[-WAIT_STREAK:])
            return
        guard["wait_streak"] = 0
        if progress:
            guard["streak"], guard["rethink"] = 0, False
            return
        key = action_key(action)
        guard["no_progress"][key] = guard["no_progress"].get(key, 0) + 1
        guard["streak"] += 1
        evidence = [{k: h.get(k) for k in ("step", "action", "region", "result", "url")} for h in history[-4:]]
        if guard["rethink"] or guard["streak"] >= NO_PROGRESS_STREAK:
            self.stop("no_progress_loop", evidence)
        elif guard["no_progress"][key] >= NO_PROGRESS_REPEATS:
            guard["excluded"][key] = f"tried {guard['no_progress'][key]}× without progress"
            guard["rethink"] = True

    def stop(self, reason, evidence):
        state = self.state
        state["status"] = "blocked"
        state["stop_reason"] = reason
        state["stop_evidence"] = evidence
        if state.get("started_at") is not None:
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()


def new_guard(page):
    return {"seen": {state_key(page): 1}, "excluded": {}, "mutations": {}, "no_progress": {}, "refusals": [],
            "streak": 0, "wait_streak": 0, "rethink": False}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

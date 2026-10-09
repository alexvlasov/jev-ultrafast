"""Offline contracts for ux-audit evidence and verdicts. Browser behaviour lives in scripts/check_ux_regressions.py."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "ux_audit", Path(__file__).parents[1] / "skills/ux-audit/scripts/ux_audit.py"
)
ux_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ux_audit)


def decision(target_probabilities, labels, operation="CLICK", operation_p=0.95, regions=None):
    return {
        "step": 3,
        "operation": operation,
        "target": max(target_probabilities, key=target_probabilities.get) if target_probabilities else None,
        "operation_probabilities": {operation: operation_p, "WAIT": 1 - operation_p},
        "target_probabilities": target_probabilities,
        "request": {"questions": {"click_target": {
            "criteria": {k: {"element": f"[{k}] {v}", "region": (regions or {}).get(k, "main")}
                         for k, v in labels.items()},
        }}},
        "latency_ms": 300,
        "elapsed_ms": 1000,
    }


def facts(url="https://shop.test/", text="", title="Shop", login=False):
    return {"url": url, "title": title, "text": text, "login_form_visible": login}


def verdict(expect, start, end, status="done", stop=None, auth=None):
    result = {"status": status, "stop_reason": stop, "auth": auth or {},
              "verification": ux_audit.evaluate_expectations(expect, start, end)}
    return ux_audit.classify(result)[:2]


def test_text_present_before_any_action_is_not_success():
    expect = {"text_contains": ["Order confirmed"]}
    page = facts(text="Order confirmed #1001")
    assert verdict(expect, page, page) == ("inconclusive", "scenario")


def test_success_requires_a_change_from_the_baseline():
    expect = {"text_contains": ["Added to cart"], "url_contains": "/cart"}
    assert verdict(expect, facts(), facts("https://shop.test/cart", "Added to cart")) == ("passed", None)


def test_agent_done_without_postconditions_is_a_failure_not_a_pass():
    assert verdict({"text_contains": ["Thanks"]}, facts(), facts(text="Form")) == ("failed", "unknown")


def test_missing_postconditions_never_pass():
    result = {"status": "done", "verification": ux_audit.evaluate_expectations({}, facts(), facts())}
    assert ux_audit.classify(result)[:2] == ("inconclusive", "unknown")


def test_budget_exhaustion_is_attributed_to_the_agent():
    assert verdict({"text_contains": ["Thanks"]}, facts(), facts(), "blocked", "time_budget") == ("blocked", "agent")


def test_signed_out_start_is_environment_not_site():
    auth = {"required": True, "login_form_visible_at_start": True}
    assert verdict({"text_contains": ["Published"]}, facts(login=True), facts(login=True), "blocked",
                   auth=auth) == ("blocked", "environment")


def test_a_loop_stop_leaves_the_cause_to_the_report_author():
    assert verdict({"text_contains": ["x"]}, facts(), facts(), "blocked", "no_progress_loop") == ("blocked", "unknown")


def test_text_absent_and_url_changed_are_compared_with_the_start():
    v = ux_audit.evaluate_expectations({"text_absent": ["Sign in"], "url_changed": True},
                                       facts(text="Sign in"), facts("https://shop.test/me", "My account"))
    assert v["all_passed"] and v["changed_from_baseline"] and not v["already_satisfied_at_start"]


def test_competing_controls_are_ambiguous():
    d = ux_audit.decision_summary(decision({"1": 0.5, "2": 0.45, "3": 0.05},
                                           {"1": "Category", "2": "Category", "3": "Help"},
                                           regions={"1": "header › search", "2": "main › form", "3": "footer"}))
    assert d["ambiguous"] and d["step"] == 3
    assert d["target_alternatives"][0]["option"] == "[1] Category (header › search)"


def test_same_control_seen_twice_is_not_ambiguous():
    d = ux_audit.decision_summary(decision({"3": 0.52, "4": 0.42, "5": 0.06}, {"3": "Eiffel", "4": "Eiffel", "5": "x"}))
    assert not d["ambiguous"]


def test_doubt_about_stopping_is_not_reported_as_an_interface_problem():
    d = ux_audit.decision_summary(decision({}, {}, operation="DONE", operation_p=0.55))
    assert d["uncertain_stop"] and not d["ambiguous"]


def test_repeated_errors_are_grouped():
    events = [{"kind": "exception", "message": "TypeError: cart_items", "after_step": i} for i in range(37)]
    events.append({"kind": "console.error", "message": "401 /users/me", "after_step": 1})
    grouped = ux_audit.dedupe(events)
    assert [(g["count"], g["first_after_step"]) for g in grouped] == [(37, 0), (1, 1)]


def test_cost_only_from_gateway_reports():
    state = {"decisions": [{"model": "typesafe-ai/jev", "usage": {"input_tokens": 10, "output_tokens": 2}}],
             "text_calls": []}
    assert ux_audit.usage_totals(state)["gateway_cost_usd"] is None
    state["decisions"][0]["usage"]["cost"] = 0.00001
    totals = ux_audit.usage_totals(state)
    assert totals["gateway_cost_usd"] == 0.00001 and totals["gateway_cost_complete"]


def test_summary_states_outcome_cause_and_metric_scope():
    page = {"viewport": [1120, 780], "issues": {"low_contrast_text": {"count": 2, "examples": []}},
            "structure": ["no visible h1"], "navigation": "client-side route change", "ready": {"reason": "settled"},
            "auth": {"login_form_visible": False}, "document_metrics": None,
            "route_diagnostics": {"layout_shift_sum": 0.04, "long_tasks": 0},
            "document_metrics_note": "belong to the document load of https://example.com/"}
    report = {
        "schema_version": 2, "site": "https://example.com", "started": "now", "elapsed_s": 3.0,
        "scenarios": [{
            "name": "subscribe", "goal": "Subscribe", "status": "blocked", "outcome": "blocked", "cause": "unknown",
            "outcome_evidence": "stopped", "stop_reason": "no_progress_loop", "stop_evidence": [], "elapsed_ms": 2000,
            "verification": {"checks": [{"check": "text contains 'Thanks'", "at_start": False, "at_end": False}]},
            "steps": [{"step": 0}, {"step": 1, "operation": "CLICK", "action": "Subscribe", "region": "main › form",
                                    "text": None, "result": "none", "progress": False, "settle_ms": 1000,
                                    "mutating": True, "wait": {"reason": "no_effect"}}],
            "console": [{"kind": "exception", "message": "boom", "count": 3}], "decisions": [],
            "signals": {"actions": 1, "ambiguous_decisions": 0, "no_progress_actions": 1, "unique_errors": 1,
                        "repeated_actions": []},
        }],
        "pages": {"https://example.com/b": {"desktop": page}},
    }
    text = ux_audit.summarize(report)
    assert "| subscribe | blocked | unknown | no_progress_loop |" in text
    assert "→ none (no_effect, 1000 ms) · no progress · data-changing" in text
    assert "3× [exception] boom" in text
    assert "route diagnostics (not CWV)" in text and "low_contrast_text 2" in text


def test_transient_feedback_counts_only_if_it_appeared_after_the_start():
    v = ux_audit.evaluate_expectations({"text_seen": ["Added to cart"]}, facts(), facts(text="Lamp"),
                                       observed=["Lamp", "Lamp Added to cart"])
    assert v["all_passed"] and v["changed_from_baseline"]
    v = ux_audit.evaluate_expectations({"text_seen": ["Added to cart"]}, facts(), facts(text="Lamp"), observed=["Lamp"])
    assert not v["all_passed"]


def test_required_file_upload_is_an_agent_limit():
    result = {"status": "blocked", "stop_reason": None, "file_upload_pending": True,
              "verification": ux_audit.evaluate_expectations({"text_contains": ["Revisión"]}, facts(), facts())}
    assert ux_audit.classify(result)[:2] == ("blocked", "agent")


def test_schema_1_results_still_summarize():
    old = {"site": "https://example.com", "started": "then", "elapsed_s": 1.0, "pages": {},
           "scenarios": [{"name": "s", "goal": "g", "status": "done", "verified": True, "elapsed_ms": 1000,
                          "steps": [{"step": 0}], "console": [{"kind": "exception", "message": "boom"}],
                          "signals": {"actions": 0, "ambiguous_decisions": 0, "repeated_actions": []}}]}
    text = ux_audit.summarize(old)
    assert "Schema 1" in text and "1× [exception] boom" in text

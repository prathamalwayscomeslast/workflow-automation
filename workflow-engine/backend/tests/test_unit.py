"""Fast tests that need no database."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.conditions import evaluate, parse_expression, render  # noqa: E402
from app.nl import rules_parse  # noqa: E402
from app.schemas import WorkflowSpec, parse_duration  # noqa: E402
from app.yaml_compiler import compile_yaml  # noqa: E402


def test_durations() -> None:
    assert parse_duration("1 day") == 86400
    assert parse_duration("2h 30m") == 9000
    assert parse_duration("45s") == 45
    assert parse_duration(10) == 10
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_conditions_no_eval() -> None:
    ctx = {"trigger": {"amount": 600, "country": "IN", "tags": ["vip"]}}
    assert evaluate({"field": "amount", "op": "gt", "value": 500}, ctx)
    assert not evaluate({"field": "trigger.amount", "op": "lte", "value": 500}, ctx)
    assert evaluate({"all": [{"field": "country", "op": "in", "value": ["IN", "US"]},
                             {"field": "tags", "op": "contains", "value": "vip"}]}, ctx)
    assert evaluate({"not": {"field": "missing", "op": "exists"}}, ctx)
    expr = parse_expression("${{ amount >= 600 && country == 'IN' || tags contains 'x' }}")
    assert evaluate(expr, ctx)
    with pytest.raises(ValueError):
        parse_expression("__import__('os').system('x')")


def test_templates_keep_types() -> None:
    ctx = {"trigger": {"amount": 600, "name": "Ada"}}
    assert render("{{trigger.amount}}", ctx) == 600
    assert render("Hi {{trigger.name}}, {{trigger.missing}}!", ctx) == "Hi Ada, !"


def test_brief_example_sentence() -> None:
    spec, _ = rules_parse("When a new order arrives, if it is over $500, wait one day, then send a follow-up message.")
    assert spec.trigger.event == "order.created"
    assert spec.dump_trigger()["conditions"] == {"field": "amount", "op": "gt", "value": 500}
    types = [s.type for s in spec.steps]
    assert types == ["wait", "send_email"]
    assert parse_duration(spec.steps[0].duration) == 86400


def test_spec_rejects_bad_graph() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate({"name": "x", "trigger": {"event": "e"}, "steps": [
            {"id": "a", "type": "branch", "condition": {"field": "x", "op": "eq", "value": 1}, "then": "nope"}]})
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate({"name": "x", "trigger": {"event": "e"}, "steps": [
            {"id": "a", "type": "wait", "duration": "1 day"}, {"id": "a", "type": "wait", "duration": "1s"}]})


def test_yaml_pipeline_compiles() -> None:
    spec = compile_yaml("""
name: GH style
on:
  event: order.created
  if: amount > 500
jobs:
  notify:
    needs: score
    steps:
      - if: steps.score.tier == "gold"
        send: { to: "{{trigger.email}}", body: "VIP" }
  score:
    steps:
      - id: score
        uses: score_order
        with: { amount: "${{ trigger.amount }}" }
      - wait: 1 day
""")
    types = [s.type for s in spec.steps]
    assert types == ["task", "wait", "branch", "send_email"]  # score job first (needs)
    assert spec.steps[2].condition.field == "steps.score.tier"
    assert spec.steps[0].params == {"amount": "{{trigger.amount}}"}

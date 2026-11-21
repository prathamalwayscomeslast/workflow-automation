"""Safe condition evaluation and templating. No eval(): conditions are data
(field / operator / value), and the string expression parser below only produces that data."""
from __future__ import annotations

import json
import re
from typing import Any

from ..schemas import AllOf, AnyOf, Comparison, Not

_MISSING = object()


def resolve(context: dict[str, Any], path: str) -> Any:
    """Dot-path lookup. Falls back to context['trigger'] so 'amount' == 'trigger.amount'."""
    def walk(obj: Any, parts: list[str]) -> Any:
        for p in parts:
            if isinstance(obj, dict) and p in obj:
                obj = obj[p]
            elif isinstance(obj, list) and p.isdigit() and int(p) < len(obj):
                obj = obj[int(p)]
            else:
                return _MISSING
        return obj

    parts = [p for p in path.strip().split(".") if p]
    val = walk(context, parts)
    if val is _MISSING and isinstance(context.get("trigger"), dict):
        val = walk(context["trigger"], parts)
    return val


def _num(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return v
    return v


def _compare(op: str, left: Any, right: Any) -> bool:
    if op == "exists":
        return left is not _MISSING and left is not None
    if op == "not_exists":
        return left is _MISSING or left is None
    if left is _MISSING:
        return False
    try:
        if op == "eq":
            return left == right or _num(left) == _num(right)
        if op == "ne":
            return not (left == right or _num(left) == _num(right))
        if op in ("gt", "gte", "lt", "lte"):
            a, b = _num(left), _num(right)
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        if op == "in":
            return left in (right or [])
        if op == "not_in":
            return left not in (right or [])
        if op == "contains":
            return right in left if isinstance(left, (str, list, dict)) else False
    except TypeError:
        return False
    raise ValueError(f"unknown operator {op}")


def evaluate(cond: Any, context: dict[str, Any]) -> bool:
    if cond is None:
        return True
    if isinstance(cond, dict):  # raw JSON from the DB
        from pydantic import TypeAdapter
        from ..schemas import Condition

        cond = TypeAdapter(Condition).validate_python(cond)
    if isinstance(cond, Comparison):
        return _compare(cond.op, resolve(context, cond.field), cond.value)
    if isinstance(cond, AllOf):
        return all(evaluate(c, context) for c in cond.all)
    if isinstance(cond, AnyOf):
        return any(evaluate(c, context) for c in cond.any)
    if isinstance(cond, Not):
        return not evaluate(cond.not_, context)
    raise TypeError(f"not a condition: {cond!r}")


# --------------------------------------------------------------------------- templates

_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def render(template: Any, context: dict[str, Any]) -> Any:
    """'Hi {{trigger.name}}' -> 'Hi Ada'. A string that is exactly one placeholder keeps the
    value's type (so params like {"amount": "{{trigger.amount}}"} stay numeric)."""
    if isinstance(template, str):
        whole = _TEMPLATE_RE.fullmatch(template.strip())
        if whole:
            v = resolve(context, whole.group(1))
            return None if v is _MISSING else v

        def sub(m: re.Match[str]) -> str:
            v = resolve(context, m.group(1))
            if v is _MISSING or v is None:
                return ""
            return v if isinstance(v, str) else json.dumps(v)

        return _TEMPLATE_RE.sub(sub, template)
    if isinstance(template, dict):
        return {k: render(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, context) for v in template]
    return template


# --------------------------------------------------------------------------- expressions
# Tiny, eval-free parser for GitHub-Actions-style `if:` strings, e.g.
#   "amount > 500", "${{ trigger.amount >= 500 && trigger.country == 'IN' }}"
# Grammar: or_expr := and_expr ('||' and_expr)* ; and_expr := atom ('&&' atom)* ;
#          atom := '!'? (path OP literal | path)        (no parentheses)

_OPS = [(">=", "gte"), ("<=", "lte"), ("==", "eq"), ("!=", "ne"), (">", "gt"), ("<", "lt"),
        (" not in ", "not_in"), (" in ", "in"), (" contains ", "contains")]


def _literal(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return json.loads(raw.replace("'", '"'))
    except ValueError:
        return raw


def _atom(expr: str) -> dict[str, Any]:
    expr = expr.strip()
    if expr.startswith("!") and not expr.startswith("!="):
        return {"not": _atom(expr[1:])}
    for sym, op in _OPS:
        if sym in expr:
            left, right = expr.split(sym, 1)
            path = left.strip()
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", path):
                raise ValueError(f"invalid field in condition: {path!r}")
            return {"field": path, "op": op, "value": _literal(right)}
    if re.fullmatch(r"[A-Za-z0-9_.\-]+", expr):
        return {"field": expr, "op": "exists"}
    raise ValueError(f"cannot parse condition: {expr!r}")


def parse_expression(expr: str) -> dict[str, Any]:
    s = expr.strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", s, re.S)
    if m:
        s = m.group(1).strip()
    if "(" in s or ")" in s:
        raise ValueError("parentheses are not supported in conditions; use all/any objects")
    ors = [p for p in s.split("||")]
    branches = []
    for part in ors:
        ands = [_atom(a) for a in part.split("&&")]
        branches.append(ands[0] if len(ands) == 1 else {"all": ands})
    return branches[0] if len(branches) == 1 else {"any": branches}

"""Plain English -> WorkflowSpec.

1. If ANTHROPIC_API_KEY is set: ask Claude for JSON constrained by the WorkflowSpec JSON
   schema (structured outputs), validate with the SAME Pydantic model the engine uses, and on
   invalid output retry once with the validation errors fed back. Still invalid -> rejected.
2. Otherwise (or if the API is unreachable): a deterministic rule-based parser that covers
   the common shapes ("when X arrives, if amount over N, wait D, then send a message").
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from .config import get_settings
from .schemas import WorkflowSpec

log = logging.getLogger("nl")

SYSTEM_PROMPT = """You convert a customer's plain-English automation request into a workflow definition.
Return ONLY a JSON object matching the provided schema.

Semantics:
- trigger.event: dotted event type, e.g. "order.created", "user.signed_up", "payment.failed", "ticket.created".
- trigger.conditions: filters on the event payload, e.g. {"field": "amount", "op": "gt", "value": 500}.
  Combine with {"all": [...]}, {"any": [...]}, {"not": {...}}. Ops: eq ne gt gte lt lte in not_in contains exists not_exists.
- steps run in order. Step ids are short snake_case and unique.
  - {"id","type":"task","task": one of http|echo|set|score_order|simulate_work, "params": {...}}
  - {"id","type":"wait","duration":"1 day"}  (durations like "30 minutes", "2 hours", "1 day")
  - {"id","type":"branch","condition":{...},"then":"<step id or end>","else":"<step id or end>"}
  - {"id","type":"send_message","channel":"email|sms|slack|webhook","to":"{{trigger.email}}","body":"..."}
- Templates use {{path}} into the run context: trigger.<payload field>, steps.<step id>.<output field>.
- A condition that decides WHETHER the workflow should run at all belongs in trigger.conditions, not a branch.
- Default recipient: "{{trigger.email}}" (use "{{trigger.phone}}" for sms). Write a short, friendly message body.
"""

_ALLOWED_SCHEMA_KEYS = {"type", "properties", "required", "items", "anyOf", "enum", "const", "$ref",
                        "$defs", "description", "additionalProperties"}


def _clean_schema(node: Any) -> Any:
    """Drop JSON-schema keywords structured outputs may not accept (pattern, min/max, defaults...)."""
    if isinstance(node, dict):
        out = {k: _clean_schema(v) for k, v in node.items() if k in _ALLOWED_SCHEMA_KEYS}
        if "properties" in node:
            out["properties"] = {k: _clean_schema(v) for k, v in node["properties"].items()}
            out["additionalProperties"] = False
        if "$defs" in node:
            out["$defs"] = {k: _clean_schema(v) for k, v in node["$defs"].items()}
        if not out:  # `Any` -> allow scalars/arrays/objects
            return {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"},
                              {"type": "array", "items": {"type": "string"}}]}
        return out
    if isinstance(node, list):
        return [_clean_schema(x) for x in node]
    return node


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def _claude(text: str) -> WorkflowSpec:
    import anthropic

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key, timeout=60, max_retries=1)
    schema = _clean_schema(WorkflowSpec.model_json_schema(by_alias=True))
    messages: list[dict[str, Any]] = [{"role": "user", "content": f"Request:\n{text}"}]
    use_schema = True
    last_err: Exception | None = None
    for attempt in range(2):  # first try + one retry with the validation errors
        kwargs: dict[str, Any] = dict(model=s.anthropic_model, max_tokens=4000,
                                      system=SYSTEM_PROMPT, messages=messages)
        if use_schema:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        try:
            resp = client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            if not use_schema:
                raise
            log.info("schema-constrained request rejected (%s); retrying without output_config", e)
            use_schema = False
            kwargs.pop("output_config")
            resp = client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise ValueError("model declined to generate this workflow")
        raw = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return WorkflowSpec.model_validate(_extract_json(raw))
        except (ValidationError, ValueError) as e:
            last_err = e
            log.info("Claude output invalid on attempt %s: %s", attempt + 1, e)
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"That JSON failed validation:\n{e}\nReturn corrected JSON only."}]
    raise ValueError(f"generated workflow failed validation twice: {last_err}")


# --------------------------------------------------------------------------- rule-based fallback

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15,
              "twenty": 20, "thirty": 30, "forty-five": 45, "sixty": 60}
_EVENT_NOUNS = {
    "order": "order.created", "purchase": "order.created", "signup": "user.signed_up",
    "sign-up": "user.signed_up", "user": "user.signed_up", "customer": "customer.created",
    "payment": "payment.received", "ticket": "ticket.created", "lead": "lead.created",
    "subscription": "subscription.created", "invoice": "invoice.created", "review": "review.created",
}
_CMP = [
    (r"(?:over|more than|greater than|above|exceeds?|larger than|bigger than)", "gt"),
    (r"(?:at least|minimum of|or more)", "gte"),
    (r"(?:under|less than|below|smaller than)", "lt"),
    (r"(?:at most|no more than|up to)", "lte"),
]
_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"


def _num(tok: str) -> float:
    tok = tok.lower()
    return float(_NUM_WORDS[tok]) if tok in _NUM_WORDS else float(tok)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40] or "step"


def rules_parse(text: str) -> tuple[WorkflowSpec, list[str]]:
    notes: list[str] = []
    t = " ".join(text.strip().split())
    low = t.lower()

    # Trigger event
    event = None
    m = re.search(r"\b(?:when|whenever|every time|each time|if|on)\s+(?:a|an|the)?\s*(?:new\s+)?([a-z\-]+)", low)
    for noun, ev in _EVENT_NOUNS.items():
        if re.search(rf"\b{noun}s?\b", low[: (m.end() + 40) if m else 80]):
            event = ev
            break
    if event is None and m:
        event = f"{m.group(1).rstrip('s')}.created"
    if event is None:
        event = "manual"
        notes.append("no trigger event recognised; using 'manual'")

    # Split into clauses, keeping order
    clauses = [c.strip(" .") for c in re.split(r",\s*(?:and\s+)?(?:then\s+)?|\bthen\b|\band then\b|;|\.\s+", low) if c and c.strip(" .")]

    conditions: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    used: set[str] = set()

    def add(step: dict[str, Any]) -> None:
        base = step["id"]
        i = 2
        while step["id"] in used:
            step["id"] = f"{base}_{i}"
            i += 1
        used.add(step["id"])
        steps.append(step)

    for c in clauses:
        cond_hit = False
        for pat, op in _CMP:
            cm = re.search(rf"{pat}\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?", c) or (
                re.search(rf"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\s*{pat}", c) if op == "gte" else None)
            if cm and not re.search(r"\bwait|after|delay|later\b", c):
                val = float(cm.group(1).replace(",", "")) * (1000 if cm.group(2) else 1)
                fm = re.search(r"\b(amount|total|price|value|quantity|score|age|count)\b", c)
                field = fm.group(1) if fm else "amount"
                field = "amount" if field in ("total", "price", "value") else field
                conditions.append({"field": field, "op": op, "value": int(val) if val.is_integer() else val})
                cond_hit = True
                break
        if cond_hit:
            continue

        wm = re.search(rf"\b(?:wait|after|delay(?: for)?|pause(?: for)?)\s+(?:for\s+)?(\d+(?:\.\d+)?|[a-z\-]+)\s+{_UNIT}", c)
        if wm:
            try:
                n = _num(wm.group(1))
                unit = wm.group(2)
                add({"id": "wait", "type": "wait", "duration": f"{n:g} {unit}"})
                continue
            except (KeyError, ValueError):
                pass

        sm = re.search(r"\b(send|email|text|notify|message|ping)\b", c)
        if sm:
            channel = "sms" if re.search(r"\b(sms|text message|text)\b", c) else (
                "slack" if "slack" in c else "email")
            kind = re.search(r"(follow[- ]up|thank[- ]you|reminder|welcome|confirmation|receipt|alert)", c)
            label = kind.group(1).replace(" ", "-") if kind else "update"
            to = "{{trigger.phone}}" if channel == "sms" else ("#alerts" if channel == "slack" else "{{trigger.email}}")
            body = {
                "follow-up": "Hi {{trigger.name}}, just following up on your recent order. Anything we can help with?",
                "thank-you": "Thank you for your order, {{trigger.name}}!",
                "welcome": "Welcome aboard, {{trigger.name}}!",
                "reminder": "Friendly reminder from our team.",
            }.get(label, f"Automated {label} message.")
            add({"id": f"send_{_slug(label)}", "type": "send_message", "channel": channel, "to": to, "body": body})
            continue

        um = re.search(r"(https?://\S+)", t if "http" in c else "")
        if re.search(r"\b(call|post|webhook|request)\b", c) and um:
            add({"id": "call_webhook", "type": "task", "task": "http",
                 "params": {"method": "POST", "url": um.group(1).rstrip(".,"), "json": {"event": "{{event}}", "data": "{{trigger}}"}}})
            continue
        if re.search(r"\b(score|tier|classify)\b", c):
            add({"id": "score", "type": "task", "task": "score_order", "params": {"amount": "{{trigger.amount}}"}})
            continue
        if c and not re.search(r"\b(when|whenever|arrives?|comes in|is created|new)\b", c):
            notes.append(f"ignored clause: {c!r}")

    if not steps:
        raise ValueError("could not find any steps (try: 'wait 1 day', 'send a follow-up email')")
    trigger: dict[str, Any] = {"event": event}
    if conditions:
        trigger["conditions"] = conditions[0] if len(conditions) == 1 else {"all": conditions}
    name = t[:80] + ("..." if len(t) > 80 else "")
    spec = WorkflowSpec.model_validate({"name": name, "description": t, "trigger": trigger, "steps": steps})
    return spec, notes


def generate(text: str) -> tuple[WorkflowSpec, str, list[str]]:
    """Returns (spec, generator, notes). Raises ValueError if the request can't be turned
    into a valid workflow."""
    if get_settings().anthropic_api_key:
        try:
            return _claude(text), "claude", []
        except ValueError:
            raise
        except Exception as e:  # network/auth problems: degrade to rules instead of failing
            log.warning("Claude unavailable (%s); using rule-based parser", e)
            spec, notes = rules_parse(text)
            return spec, "rules", [f"Claude unavailable ({type(e).__name__}); used rule-based parser", *notes]
    spec, notes = rules_parse(text)
    return spec, "rules", notes

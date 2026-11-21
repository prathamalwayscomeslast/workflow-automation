"""GitHub-Actions-style YAML -> WorkflowSpec.

    name: High value order follow-up
    on:
      event: order.created
      if: amount > 500
    jobs:                      # optional; jobs run sequentially in `needs` order
      followup:
        steps:
          - id: score
            uses: score_order          # task name (http, echo, set, score_order, ...)
            with: { amount: "${{ trigger.amount }}" }
          - wait: 1 day
          - if: steps.score.tier == "gold"   # step-level if => skipped when false
            send:
              to: "{{trigger.email}}"
              body: "Thanks for your big order!"

Top-level `steps:` works too (single implicit job). Step keys:
  uses/with   -> task step          wait: <duration>   -> wait step
  send: {...} -> send_message step  if/then/else (no action) -> explicit branch
  if: on an action step compiles to a branch that skips the step when false.
The output is validated by the same WorkflowSpec the engine runs.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from .engine.conditions import parse_expression
from .schemas import WorkflowSpec

_GH_EXPR = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")


def _templ(v: Any) -> Any:
    """Accept GitHub's ${{ x }} as well as {{x}} in strings."""
    if isinstance(v, str):
        return _GH_EXPR.sub(lambda m: "{{" + m.group(1) + "}}", v)
    if isinstance(v, dict):
        return {k: _templ(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_templ(x) for x in v]
    return v


def _cond(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict,)):
        return v
    if isinstance(v, bool):
        return {"field": "event", "op": "exists"} if v else {"not": {"field": "event", "op": "exists"}}
    return parse_expression(str(v))


def _ordered_jobs(jobs: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in order:
            return
        if name in visiting:
            raise ValueError(f"cycle in jobs.needs at {name!r}")
        if name not in jobs:
            raise ValueError(f"unknown job in needs: {name!r}")
        visiting.add(name)
        needs = jobs[name].get("needs") or []
        for dep in [needs] if isinstance(needs, str) else needs:
            visit(dep)
        visiting.discard(name)
        order.append(name)

    for n in jobs:
        visit(n)
    return [(n, jobs[n] or {}) for n in order]


def compile_yaml(source: str) -> WorkflowSpec:
    doc = yaml.safe_load(source)
    if not isinstance(doc, dict):
        raise ValueError("YAML must be a mapping")
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    on = doc.get("on", doc.get(True, doc.get("trigger")))
    if isinstance(on, str):
        trigger: dict[str, Any] = {"event": on}
    elif isinstance(on, dict):
        trigger = {"event": on.get("event") or on.get("type")}
        cond = on.get("if") or on.get("conditions")
        if cond is not None:
            trigger["conditions"] = _cond(cond)
    else:
        raise ValueError("`on:` must be an event name or {event, if}")

    if "jobs" in doc:
        raw_steps: list[tuple[str | None, dict[str, Any]]] = []
        for job_name, job in _ordered_jobs(doc["jobs"] or {}):
            for st in job.get("steps") or []:
                raw_steps.append((job_name, st))
    else:
        raw_steps = [(None, st) for st in doc.get("steps") or []]
    if not raw_steps:
        raise ValueError("no steps defined")

    # First pass: assign ids so `then:`/`else:`/`next:` can reference them.
    ids: list[str] = []
    for i, (job, st) in enumerate(raw_steps):
        if not isinstance(st, dict):
            raise ValueError(f"step {i + 1} must be a mapping")
        sid = str(st.get("id") or f"step_{i + 1}")
        ids.append(f"{job}__{sid}" if job and len(doc.get("jobs", {})) > 1 else sid)
    idmap = {str(st.get("id")): ids[i] for i, (_, st) in enumerate(raw_steps) if st.get("id")}

    def ref(t: Any) -> Any:
        return None if t is None else ("end" if t == "end" else idmap.get(str(t), str(t)))

    steps: list[dict[str, Any]] = []
    for i, (_, st) in enumerate(raw_steps):
        sid = ids[i]
        nxt = ids[i + 1] if i + 1 < len(ids) else "end"
        common = {"name": st.get("name")} if st.get("name") else {}
        action: dict[str, Any] | None = None
        if "uses" in st or "run" in st:
            task_name = str(st.get("uses") or st.get("run")).split("@")[0].removeprefix("task/")
            action = {"type": "task", "task": task_name, "params": _templ(st.get("with") or {})}
        elif "wait" in st:
            action = {"type": "wait", "duration": st["wait"]}
        elif "send" in st:
            send = st["send"] if isinstance(st["send"], dict) else {"body": str(st["send"])}
            action = {"type": "send_message", "channel": send.get("channel", "email"),
                      "to": _templ(send.get("to", "{{trigger.email}}")), "body": _templ(send.get("body", "")),
                      **({"subject": _templ(send["subject"])} if send.get("subject") else {})}

        if action is None:
            if "if" not in st or "then" not in st:
                raise ValueError(f"step {sid!r} needs one of uses/wait/send, or if+then")
            steps.append({"id": sid, "type": "branch", "condition": _cond(st["if"]),
                          "then": ref(st["then"]), **({"else": ref(st["else"])} if st.get("else") else {}),
                          **common})
            continue

        if st.get("next"):
            action["next"] = ref(st["next"])
        if "if" in st:  # GitHub semantics: run the step only when the condition holds
            steps.append({"id": f"{sid}__if", "type": "branch", "condition": _cond(st["if"]),
                          "then": sid, "else": nxt})
        steps.append({"id": sid, **action, **common})

    return WorkflowSpec.model_validate({
        "name": doc.get("name") or "Untitled pipeline",
        "description": doc.get("description"),
        "trigger": trigger,
        "steps": steps,
    })

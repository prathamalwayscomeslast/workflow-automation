import { useState } from "react";
import { api, type Message, type RunDetail, type RunStep, type StepDef, type Workflow } from "../api";
import { PipelineGraph, stepStates } from "../Graph";
import { ago, between, STEP_LABEL, stepTitle, useNow, usePoll } from "../lib";
import { IconChevron, IconMail, JsonView, StatusIcon, StatusPill, StepIcon } from "../ui";

export function RunPage({ workflow, runId }: { workflow?: Workflow; runId: number }) {
  const [run, , err] = usePoll<RunDetail>(() => api.run(runId), 1200, [runId]);
  const [msgs] = usePoll<Message[]>(() => api.messages(runId), 1500, [runId]);
  const now = useNow(1000);

  if (err && !run) return <div className="page"><div className="err-box">{err}</div></div>;
  if (!run) return <div className="page"><div className="muted">Loading run…</div></div>;

  const steps = run.workflow_steps;
  const states = stepStates(steps, run);
  const trigger = workflow?.trigger ?? { event: String(run.context.event ?? "webhook") };
  const active = run.status === "running" || run.status === "pending" || run.status === "waiting";

  return (
    <div className="page">
      <div>
        <div className="crumbs">
          <a href="#/">Workflows</a> <span>/</span>
          <a href={`#/w/${run.workflow_id}`}>{workflow?.name ?? `Workflow ${run.workflow_id}`}</a> <span>/</span>
          <span style={{ color: "var(--ink)" }}>Run #{run.id}</span>
        </div>
        <div className="page-head">
          <div className="row" style={{ gap: 14 }}>
            <StatusIcon status={run.status} size={30} />
            <div>
              <h1 style={{ fontSize: 28 }}>Run #{run.id}</h1>
              <div className="muted small" style={{ marginTop: 4 }}>
                started {ago(run.created_at, now)} · {active ? "running for" : "took"} {between(run.created_at, run.finished_at, now)} ·{" "}
                {run.step_count}/{steps.length} steps
                {run.claim_count > 1 && <> · resumed {run.claim_count - 1}× (after waits or restarts)</>}
              </div>
            </div>
          </div>
          <StatusPill status={run.status} label={run.status === "waiting" && run.wake_at ? `Waiting · resumes ${ago(run.wake_at, now)}` : undefined} />
        </div>
      </div>

      {run.error && <div className="err-box"><b>Failed:</b> {run.error}</div>}

      <div className="card">
        <div className="card-head"><h3>Pipeline</h3><span className="muted small">{active ? "live" : "final state"}</span></div>
        <div className="card-body" style={{ paddingTop: 14, paddingBottom: 8 }}>
          <PipelineGraph trigger={trigger} steps={steps} run={run} />
        </div>
      </div>

      <div className="grid-3">
        <div className="card">
          <div className="card-head"><h3>Steps</h3><span className="muted small">audit trail · one committed record per step</span></div>
          <div className="steps">
            {steps.map((s, i) => (
              <StepRow key={s.id} step={s} state={states[i].state} rec={states[i].rec} run={run} now={now} />
            ))}
          </div>
        </div>

        <div className="stack" style={{ gap: 22 }}>
          <div className="card">
            <div className="card-head"><h3>Emails &amp; messages</h3><span className="muted small">{msgs?.length ?? 0}</span></div>
            {msgs?.length ? msgs.map((m) => <MailRow key={m.idempotency_key} m={m} />) : <div className="empty" style={{ padding: 28 }}>No emails queued yet.</div>}
          </div>
          <div className="card">
            <div className="card-head"><h3>Trigger payload</h3></div>
            <div className="card-body"><JsonView value={run.context.trigger ?? {}} /></div>
          </div>
        </div>
      </div>

      <div className="card">
        <details className="raw">
          <summary>Raw run context (trigger + every step's output)</summary>
          <JsonView value={run.context} />
        </details>
      </div>
    </div>
  );
}

function StepRow({ step, state, rec, run, now }: { step: StepDef; state: string; rec?: RunStep; run: RunDetail; now: number }) {
  const [open, setOpen] = useState(state === "failed");
  const meta =
    state === "waiting" && run.wake_at ? `resumes ${ago(run.wake_at, now)}`
    : rec ? between(rec.started_at, rec.finished_at, now)
    : state === "skipped" ? "skipped" : state === "idle" ? "not run yet" : "";
  return (
    <div className={`step ${open ? "open" : ""}`}>
      <div className="step-head" onClick={() => setOpen(!open)}>
        <IconChevron size={14} className="chev" />
        <StatusIcon status={state} size={18} />
        <span className="gicon" style={{ width: 24, height: 24 }}><StepIcon type={step.type} size={13} /></span>
        <span className="name">{stepTitle(step)} <span className="muted small" style={{ fontWeight: 400 }}>· {STEP_LABEL[step.type]}</span></span>
        <span className="muted small mono">{meta}</span>
      </div>
      {open && (
        <div className="step-body stack" style={{ gap: 10 }}>
          {rec?.error && <div className="err-box mono">{rec.error}</div>}
          <div className="muted tiny">Definition</div>
          <JsonView value={step} />
          {rec?.output && (
            <>
              <div className="muted tiny">Output · {rec.worker_id ? `worker ${rec.worker_id}` : ""}</div>
              <JsonView value={rec.output} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

function MailRow({ m }: { m: Message }) {
  const status = m.outbox_status === "sent" ? "completed" : m.outbox_status === "failed" ? "failed" : "pending";
  return (
    <div className="mail">
      <div className="mail-icon"><IconMail size={16} /></div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="subject">{m.payload.subject || (m.channel === "email" ? "(no subject)" : `Demo message · ${m.channel}`)}</span>
          <StatusPill status={status} label={m.outbox_status === "sent" ? "Delivered" : m.outbox_status === "failed" ? "Failed" : "Sending"} />
        </div>
        <div className="muted small">to {m.to}{m.attempts > 1 ? ` · ${m.attempts} attempts` : ""}</div>
        <div className="body">{m.payload.body}</div>
        <div className="muted tiny mono" style={{ marginTop: 6 }} title="Idempotency key: retries can never queue this email twice">key {m.idempotency_key}</div>
      </div>
    </div>
  );
}

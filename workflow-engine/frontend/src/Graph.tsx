import type { RunDetail, RunStep, StepDef, Workflow } from "./api";
import { ago, between, condText, STEP_LABEL, stepDesc, stepTitle, useNow } from "./lib";
import { StatusIcon, StepIcon } from "./ui";

type NodeState = "idle" | "completed" | "failed" | "running" | "pending" | "waiting" | "skipped";

/** Works out each step's state for a run: executed, current, waiting, or skipped. */
export function stepStates(steps: StepDef[], run?: RunDetail): { state: NodeState; rec?: RunStep }[] {
  const out = steps.map(() => ({ state: "idle" as NodeState, rec: undefined as RunStep | undefined }));
  if (!run) return out;
  for (const rec of run.steps) out[rec.step_index] = { state: rec.status as NodeState, rec };
  const last = run.steps[run.steps.length - 1];
  if (run.status === "waiting" && last && steps[last.step_index]?.type === "wait") {
    out[last.step_index] = { ...out[last.step_index], state: "waiting" };
  } else if ((run.status === "running" || run.status === "pending") && run.current_step !== null && out[run.current_step]) {
    out[run.current_step] = { ...out[run.current_step], state: run.status };
  }
  if (run.status === "completed" || run.status === "failed") {
    out.forEach((o) => {
      if (o.state === "idle") o.state = "skipped";
    });
  }
  return out;
}

export function triggerLabel(trigger: Workflow["trigger"]): string {
  return trigger.event === "webhook" ? "Webhook" : trigger.event;
}

export function PipelineGraph({ trigger, steps, run }: { trigger: Workflow["trigger"]; steps: StepDef[]; run?: RunDetail }) {
  const now = useNow(1000);
  const states = stepStates(steps, run);

  return (
    <div className="graph">
      <div className="gnode-wrap">
        <div className="gnode trigger">
          <div className="gtop">
            <span className="gicon"><StepIcon type="trigger" /></span>
            <span className="gname">{triggerLabel(trigger)}</span>
            {run && <StatusIcon status="completed" size={16} />}
          </div>
          <div className="gtype">Trigger</div>
          <div className="gdesc">{trigger.conditions ? `when ${condText(trigger.conditions)}` : "on every request"}</div>
          {run && <div className="gmeta">{ago(run.created_at, now)}</div>}
        </div>
      </div>
      {steps.map((s, i) => {
        const { state, rec } = states[i];
        const prevDone = i === 0 ? !!run : ["completed", "waiting"].includes(states[i - 1].state);
        const branchResult = s.type === "branch" && rec?.output ? Boolean(rec.output.result) : undefined;
        return (
          <div className="gnode-wrap" key={s.id}>
            <div className={`gconn ${prevDone ? "done" : ""}`} />
            <div className={`gnode ${state}`} title={s.id}>
              <div className="gtop">
                <span className="gicon"><StepIcon type={s.type} /></span>
                <span className="gname">{stepTitle(s)}</span>
                {state !== "idle" && <StatusIcon status={state} size={16} />}
              </div>
              <div className="gtype">{STEP_LABEL[s.type]}</div>
              <div className="gdesc">{stepDesc(s)}</div>
              {s.type === "branch" && (
                <div className="gbranch">
                  <span className={branchResult === true ? "yes" : branchResult === false ? "no" : ""}>yes → {s.then}</span>
                  <span className={branchResult === false ? "yes" : branchResult === true ? "no" : ""}>no → {s.else ?? "next"}</span>
                </div>
              )}
              <div className="gmeta">
                {state === "waiting" && run?.wake_at ? `resumes ${ago(run.wake_at, now)}`
                  : rec ? between(rec.started_at, rec.finished_at, now)
                  : state === "running" ? "running…"
                  : state === "skipped" ? "skipped"
                  : s.next ? `then → ${s.next}` : ""}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** Compact row of step icons for cards. */
export function MiniSteps({ steps }: { steps: StepDef[] }) {
  return (
    <div className="mini-steps">
      <span className="mini-step trigger"><StepIcon type="trigger" size={13} /></span>
      {steps.slice(0, 9).map((s) => (
        <span key={s.id} className="row" style={{ gap: 4 }}>
          <span className="mini-line" />
          <span className="mini-step" title={stepTitle(s)}><StepIcon type={s.type} size={13} /></span>
        </span>
      ))}
      {steps.length > 9 && <span className="muted tiny">+{steps.length - 9}</span>}
    </div>
  );
}

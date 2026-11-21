import { api, type Run, type Stats, type Workflow } from "../api";
import { MiniSteps, triggerLabel } from "../Graph";
import { ago, between, go, useNow, usePoll } from "../lib";
import { Empty, IconMail, IconPlus, StatusPill, Terminal } from "../ui";

export function Dashboard({ workflows, stats }: { workflows?: Workflow[]; stats?: Stats }) {
  const [runs] = usePoll<Run[]>(() => api.runs(), 2000);
  const now = useNow(5000);
  const byId = new Map(workflows?.map((w) => [w.id, w]) ?? []);
  const lastRun = new Map<number, Run>();
  const runCount = new Map<number, number>();
  runs?.forEach((r) => {
    if (!lastRun.has(r.workflow_id)) lastRun.set(r.workflow_id, r);
    runCount.set(r.workflow_id, (runCount.get(r.workflow_id) ?? 0) + 1);
  });

  const r = stats?.runs ?? {};
  const finished = (r.completed ?? 0) + (r.failed ?? 0);
  const successRate = finished ? Math.round(((r.completed ?? 0) / finished) * 100) : null;
  const firstHook = workflows?.find((w) => w.webhook_url)?.webhook_url;

  return (
    <div className="page">
      <section className="stage">
        <div className="stage-grid">
          <div className="stack" style={{ gap: 18 }}>
            <div className="eyebrow">Durable workflow automation</div>
            <h1 style={{ fontSize: 44, maxWidth: 560 }}>When this happens, do that. Reliably.</h1>
            <p className="muted" style={{ fontSize: 16, maxWidth: 520 }}>
              Webhooks start runs, steps wait for days without holding a server, and every email goes out exactly once,
              even if the engine restarts mid-run.
            </p>
            <div className="row wrap">
              <a className="btn primary" href="#/new"><IconPlus size={15} /> New workflow</a>
              <a className="btn dark" href="#/email"><IconMail size={15} /> Connect email</a>
            </div>
            {firstHook && (
              <div style={{ maxWidth: 560 }}>
                <div className="muted small" style={{ marginBottom: 6 }}>Or trigger your latest workflow now:</div>
                <Terminal text={`curl -X POST ${firstHook} -H "content-type: application/json" -d '{"amount": 750, "email": "you@example.com"}'`} />
              </div>
            )}
          </div>
          <div className="stack" style={{ gap: 22, paddingTop: 8 }}>
            <div className="stat-row">
              <div className="stat"><b>{stats?.workflows ?? "–"}</b><span>workflows</span></div>
              <div className="stat"><b>{finished + (r.running ?? 0) + (r.waiting ?? 0) + (r.pending ?? 0)}</b><span>runs</span></div>
              <div className="stat"><b>{successRate === null ? "–" : `${successRate}%`}</b><span>success rate</span></div>
            </div>
            <div className="stat-row">
              <div className="stat"><b>{(r.running ?? 0) + (r.pending ?? 0)}</b><span>running now</span></div>
              <div className="stat"><b>{r.waiting ?? 0}</b><span>waiting</span></div>
              <div className="stat"><b>{stats?.outbox?.sent ?? 0}</b><span>emails sent</span></div>
              <div className="stat"><b style={{ color: (r.failed ?? 0) ? "#ff8a8d" : undefined }}>{r.failed ?? 0}</b><span>failed</span></div>
            </div>
          </div>
        </div>
      </section>

      <div className="page-head">
        <div>
          <h2>Workflows</h2>
          <div className="muted small" style={{ marginTop: 4 }}>Each workflow has its own webhook URL.</div>
        </div>
        <a className="btn" href="#/new"><IconPlus size={15} /> New</a>
      </div>

      {workflows && !workflows.length ? (
        <div className="card">
          <Empty title="No workflows yet">
            <p>Describe one in plain English, paste a YAML pipeline, or write JSON.</p>
            <a className="btn primary" href="#/new"><IconPlus size={15} /> Create your first workflow</a>
          </Empty>
        </div>
      ) : (
        <div className="wf-grid">
          {workflows?.map((w) => {
            const last = lastRun.get(w.id);
            return (
              <div key={w.id} className="card wf-card" onClick={() => go(`/w/${w.id}`)}>
                <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
                  <div style={{ minWidth: 0 }}>
                    <div className="name">{w.name}</div>
                    <div className="muted small" style={{ marginTop: 3 }}>{triggerLabel(w.trigger)} · {w.steps.length} steps</div>
                  </div>
                  {!w.enabled ? <StatusPill status="disabled" /> : last ? <StatusPill status={last.status} /> : <StatusPill status="idle" />}
                </div>
                <MiniSteps steps={w.steps} />
                <div className="row muted small" style={{ justifyContent: "space-between" }}>
                  <span><span className="pill">{w.source}</span>&nbsp; {runCount.get(w.id) ?? 0} recent runs</span>
                  <span>{last ? `last run ${ago(last.created_at, now)}` : `created ${ago(w.created_at, now)}`}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <h3>Recent runs</h3>
          <span className="muted small">live · refreshes every 2s</span>
        </div>
        {runs && !runs.length ? (
          <div className="empty">No runs yet. Call a workflow's webhook to start one.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr><th>Status</th><th>Run</th><th>Progress</th><th>Duration</th><th>Started</th></tr>
            </thead>
            <tbody>
              {runs?.slice(0, 15).map((run) => {
                const wf = byId.get(run.workflow_id);
                return (
                  <tr key={run.id} onClick={() => go(`/w/${run.workflow_id}/r/${run.id}`)}>
                    <td><StatusPill status={run.status} /></td>
                    <td>
                      <div className="title">{wf?.name ?? `Workflow ${run.workflow_id}`}</div>
                      <div className="sub">run #{run.id}{run.claim_count > 1 ? ` · resumed ${run.claim_count - 1}×` : ""}</div>
                    </td>
                    <td className="small">{run.step_count}/{wf?.steps.length ?? "?"} steps{run.status === "waiting" ? ` · resumes ${ago(run.wake_at, now)}` : ""}</td>
                    <td className="small mono">{between(run.created_at, run.finished_at, now)}</td>
                    <td className="small muted">{ago(run.created_at, now)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

import { useEffect, useState } from "react";
import { api, type Run, type Workflow } from "../api";
import { PipelineGraph, triggerLabel } from "../Graph";
import { ago, between, condText, copy, go, samplePayload, toast, useNow, usePoll } from "../lib";
import { Empty, IconCopy, IconLock, IconPlay, IconRefresh, IconTrash, StatusPill, Terminal } from "../ui";

export function WorkflowPage({ workflow, onChange }: { workflow: Workflow; onChange: () => void }) {
  const [runs] = usePoll<Run[]>(() => api.runs(workflow.id), 1500, [workflow.id]);
  const now = useNow(2000);
  const counts = { completed: 0, failed: 0, active: 0 };
  runs?.forEach((r) => {
    if (r.status === "completed") counts.completed++;
    else if (r.status === "failed") counts.failed++;
    else counts.active++;
  });

  const curl = workflow.webhook_url
    ? `curl -X POST ${workflow.webhook_url} -H "content-type: application/json" -d '${JSON.stringify(samplePayload(workflow.trigger.event))}'`
    : "";

  return (
    <div className="page">
      <section className="stage">
        <div className="crumbs" style={{ color: "var(--navy-muted)" }}>
          <a href="#/">Workflows</a> <span>/</span> <span style={{ color: "#fff" }}>{workflow.name}</span>
        </div>
        <div className="stage-grid">
          <div className="stack" style={{ gap: 14 }}>
            <div className="row wrap" style={{ gap: 8 }}>
              <span className="eyebrow">{triggerLabel(workflow.trigger)} trigger</span>
              {!workflow.enabled && <StatusPill status="disabled" />}
            </div>
            <h1>{workflow.name}</h1>
            {workflow.trigger.conditions && <p className="muted">Runs only when <b style={{ color: "#fff" }}>{condText(workflow.trigger.conditions)}</b></p>}
            {workflow.source === "text" && workflow.source_text && <p className="muted">“{workflow.source_text}”</p>}
            {curl && <Terminal text={curl} />}
            <div className="row wrap">
              <button className="btn dark" onClick={async () => { await api.setEnabled(workflow.id, !workflow.enabled); onChange(); toast(workflow.enabled ? "Workflow disabled" : "Workflow enabled"); }}>
                {workflow.enabled ? "Disable" : "Enable"}
              </button>
              <button className="btn dark" onClick={async () => {
                if (!confirm(`Delete "${workflow.name}" and all its runs?`)) return;
                await api.deleteWorkflow(workflow.id);
                onChange();
                go("/");
              }}><IconTrash size={14} /> Delete</button>
            </div>
          </div>
          <div className="stat-row" style={{ alignSelf: "end", justifyContent: "flex-end" }}>
            <div className="stat"><b>{runs?.length ?? "–"}</b><span>recent runs</span></div>
            <div className="stat"><b style={{ color: "var(--green)" }}>{counts.completed}</b><span>succeeded</span></div>
            <div className="stat"><b>{counts.active}</b><span>in progress</span></div>
            <div className="stat"><b style={{ color: counts.failed ? "#ff8a8d" : undefined }}>{counts.failed}</b><span>failed</span></div>
          </div>
        </div>
      </section>

      <div className="card">
        <div className="card-head">
          <h3>Pipeline</h3>
          <span className="muted small">{workflow.steps.length} steps · read left to right</span>
        </div>
        <div className="card-body" style={{ paddingTop: 14, paddingBottom: 8 }}>
          <PipelineGraph trigger={workflow.trigger} steps={workflow.steps} />
        </div>
      </div>

      <div className="grid-2">
        <TestTrigger workflow={workflow} />
        <WebhookSettings workflow={workflow} onChange={onChange} />
      </div>

      <div className="card">
        <div className="card-head">
          <h3>Runs</h3>
          <span className="muted small">live</span>
        </div>
        {runs && !runs.length ? (
          <Empty title="No runs yet">
            <p>Send a test event above, or call the webhook URL from any service.</p>
          </Empty>
        ) : (
          <table className="tbl">
            <thead>
              <tr><th>Status</th><th>Run</th><th>Current step</th><th>Duration</th><th>Started</th></tr>
            </thead>
            <tbody>
              {runs?.map((r) => {
                const cur = r.current_step !== null ? workflow.steps[r.current_step] : undefined;
                return (
                  <tr key={r.id} onClick={() => go(`/w/${workflow.id}/r/${r.id}`)}>
                    <td><StatusPill status={r.status} /></td>
                    <td>
                      <div className="title">#{r.id}</div>
                      <div className="sub">{String((r.context.trigger as Record<string, unknown> | undefined)?.email ?? "")}</div>
                    </td>
                    <td className="small">
                      {r.status === "completed" ? "Done" : r.status === "failed" ? <span style={{ color: "var(--red)" }}>{r.error?.slice(0, 60)}</span>
                        : r.status === "waiting" ? `Waiting · resumes ${ago(r.wake_at, now)}` : cur ? cur.id : "—"}
                    </td>
                    <td className="small mono">{between(r.created_at, r.finished_at, now)}</td>
                    <td className="small muted">{ago(r.created_at, now)}</td>
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

function TestTrigger({ workflow }: { workflow: Workflow }) {
  const [payload, setPayload] = useState(() => JSON.stringify(samplePayload(workflow.trigger.event), null, 2));
  const [key, setKey] = useState("");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setPayload(JSON.stringify(samplePayload(workflow.trigger.event), null, 2));
    setResult(null);
  }, [workflow.id, workflow.trigger.event]);

  const send = async () => {
    setBusy(true);
    setResult(null);
    try {
      const body = JSON.parse(payload);
      const r = workflow.webhook_url
        ? await api.callHook(workflow.webhook_url, body, key || undefined)
        : await api.sendEvent(workflow.trigger.event, body, key || undefined).then((x) => ({ started: x.run_ids.length > 0, run_id: x.run_ids[0], reason: String(x.skipped[0]?.reason ?? "") }));
      if (r.started && r.run_id) {
        toast(`Run #${r.run_id} started`);
        go(`/w/${workflow.id}/r/${r.run_id}`);
      } else {
        setResult({ ok: false, text: r.reason || "No run started" });
      }
    } catch (e) {
      setResult({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <div className="card-head">
        <h3>Test trigger</h3>
        <span className="muted small">POSTs to the webhook, like an external service would</span>
      </div>
      <div className="card-body stack">
        <div className="editor">
          <div className="editor-bar"><i /><i /><i /><span style={{ marginLeft: 8 }}>payload.json</span></div>
          <textarea style={{ minHeight: 170 }} value={payload} spellCheck={false} onChange={(e) => setPayload(e.target.value)} />
        </div>
        <div className="row">
          <input placeholder="Idempotency-Key (optional)" value={key} onChange={(e) => setKey(e.target.value)} />
          <button className="btn primary" disabled={busy} onClick={send}><IconPlay size={13} /> {busy ? "Sending…" : "Send event"}</button>
        </div>
        {result && <div className={result.ok ? "ok-box" : "err-box"}>{result.text}</div>}
      </div>
    </div>
  );
}

function WebhookSettings({ workflow, onChange }: { workflow: Workflow; onChange: () => void }) {
  const [secret, setSecret] = useState("");
  return (
    <div className="card">
      <div className="card-head">
        <h3>Webhook</h3>
        {workflow.webhook_signed ? <span className="chip"><IconLock size={12} /> signed</span> : <span className="chip">public URL</span>}
      </div>
      <div className="card-body stack">
        <label className="field">
          Endpoint URL
          <div className="row">
            <input readOnly className="mono" value={workflow.webhook_url ?? ""} onFocus={(e) => e.target.select()} />
            <button className="btn icon" title="Copy" onClick={() => workflow.webhook_url && copy(workflow.webhook_url)}><IconCopy size={14} /></button>
            <button className="btn icon" title="Rotate URL" onClick={async () => {
              if (!confirm("Generate a new URL? The current one stops working immediately.")) return;
              await api.webhookSettings(workflow.id, { rotate_token: true });
              onChange();
              toast("New webhook URL generated");
            }}><IconRefresh size={14} /></button>
          </div>
        </label>
        <p className="muted small">
          Send JSON with <code>POST</code>; its fields are available to steps as <code>{"{{trigger.field}}"}</code>. Add an{" "}
          <code>Idempotency-Key</code> header so retried deliveries never start a second run.
        </p>
        <label className="field">
          Signing secret (HMAC-SHA256)
          <div className="row">
            <input type="password" placeholder={workflow.webhook_signed ? "•••••••• (type to replace)" : "optional"} value={secret} onChange={(e) => setSecret(e.target.value)} />
            <button className="btn" onClick={async () => {
              await api.webhookSettings(workflow.id, { secret });
              setSecret("");
              onChange();
              toast(secret ? "Secret saved" : "Secret removed");
            }}>{secret ? "Save" : workflow.webhook_signed ? "Remove" : "Save"}</button>
          </div>
        </label>
        <p className="muted tiny">When set, callers must send <code>X-Signature-256: sha256=&lt;hex hmac of raw body&gt;</code>.</p>
      </div>
    </div>
  );
}

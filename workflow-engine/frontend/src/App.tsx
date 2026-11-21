import { api, type Stats, type Workflow } from "./api";
import { usePoll, useRoute, useToast } from "./lib";
import { IconBolt, IconPlus } from "./ui";
import { CreatePage } from "./views/CreatePage";
import { Dashboard } from "./views/Dashboard";
import { EmailPage } from "./views/EmailPage";
import { RunPage } from "./views/RunPage";
import { WorkflowPage } from "./views/WorkflowPage";

export default function App() {
  const route = useRoute();
  const toastMsg = useToast();
  const [workflows, reloadWorkflows, wfErr] = usePoll<Workflow[]>(api.workflows, 8000);
  const [stats] = usePoll<Stats>(api.stats, 3000);
  const [health] = usePoll(api.health, 20000);
  const [emails] = usePoll(api.emailAccounts, 20000);

  const [section, id, sub, subId] = route;
  const wf = section === "w" ? workflows?.find((w) => w.id === Number(id)) : undefined;

  let page;
  if (section === "new") page = <CreatePage onCreated={reloadWorkflows} />;
  else if (section === "email") page = <EmailPage />;
  else if (section === "w" && sub === "r" && subId) page = <RunPage workflow={wf} runId={Number(subId)} />;
  else if (section === "w" && wf) page = <WorkflowPage workflow={wf} onChange={reloadWorkflows} />;
  else if (section === "w" && workflows) page = <div className="page"><div className="err-box">Workflow {id} not found. <a href="#/">Back to workflows</a></div></div>;
  else page = <Dashboard workflows={workflows} stats={stats} />;

  const active = section === "email" ? "email" : section === "new" ? "new" : "workflows";
  const noEmail = emails && emails.length === 0;

  return (
    <>
      <nav className="nav">
        <a className="brand" href="#/">
          <span className="brand-mark"><IconBolt size={15} /></span>
          Relay
        </a>
        <div className="nav-links">
          <a href="#/" className={active === "workflows" ? "active" : ""}>Workflows</a>
          <a href="#/email" className={active === "email" ? "active" : ""}>Email</a>
          <a href="http://localhost:8000/docs" target="_blank" rel="noreferrer">API</a>
        </div>
        <div className="nav-right">
          <span className="chip" title="Worker threads claim and run due workflows">
            <span className={`dot ${wfErr ? "off" : health?.ok ? "on" : ""}`} />
            {wfErr ? "API offline" : health?.ok ? "Engine online" : "Connecting…"}
          </span>
          {health && <span className="chip" title="Plain-English workflow generator">AI: {health.nl_generator === "claude" ? "Claude" : "rules"}</span>}
          <a className="btn primary sm" href="#/new"><IconPlus size={14} /> New workflow</a>
        </div>
      </nav>
      {wfErr ? (
        <div className="banner error">Can't reach the API ({wfErr}). Start the backend: <code>uvicorn app.main:app --port 8000</code></div>
      ) : noEmail ? (
        <div className="banner"><span className="new">Setup</span> Connect a mailbox so your workflows can send email. <a href="#/email" style={{ fontWeight: 600, textDecoration: "underline" }}>Connect email →</a></div>
      ) : null}
      {page}
      {toastMsg && <div className="toast">{toastMsg}</div>}
    </>
  );
}

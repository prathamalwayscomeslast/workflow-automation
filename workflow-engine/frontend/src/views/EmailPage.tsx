import { useState } from "react";
import { api, type EmailAccount, type Json } from "../api";
import { ago, toast, usePoll } from "../lib";
import { Empty, IconLock, IconMail, IconTrash, StatusPill } from "../ui";

const PROVIDERS = [
  { id: "gmail", name: "Gmail", hint: "you@gmail.com", link: "https://myaccount.google.com/apppasswords" },
  { id: "outlook", name: "Outlook", hint: "you@outlook.com", link: "https://account.live.com/proofs/AppPassword" },
  { id: "yahoo", name: "Yahoo", hint: "you@yahoo.com", link: "https://login.yahoo.com/account/security" },
  { id: "zoho", name: "Zoho", hint: "you@zoho.com", link: "" },
  { id: "custom", name: "Other SMTP", hint: "you@yourdomain.com", link: "" },
];

export function EmailPage() {
  const [accounts, reload] = usePoll<EmailAccount[]>(api.emailAccounts, 15000);
  const [presets] = usePoll(api.emailPresets, 600000);
  const [provider, setProvider] = useState("gmail");
  const [form, setForm] = useState({ from_email: "", from_name: "", password: "", smtp_host: "", smtp_port: "587", security: "starttls" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof typeof form, v: string) => setForm({ ...form, [k]: v });
  const p = PROVIDERS.find((x) => x.id === provider)!;

  const connect = async () => {
    setBusy(true);
    setErr(null);
    try {
      const body: Json = { provider, from_email: form.from_email.trim(), label: form.from_email.trim(), from_name: form.from_name || undefined, password: form.password || undefined };
      if (provider === "custom") Object.assign(body, { smtp_host: form.smtp_host, smtp_port: Number(form.smtp_port), security: form.security });
      const a = await api.connectEmail(body);
      toast(`Connected ${a.from_email}. Check your inbox for the test email.`);
      setForm({ ...form, password: "" });
      reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <section className="stage">
        <div className="stage-grid">
          <div className="stack" style={{ gap: 12 }}>
            <div className="eyebrow">Email accounts</div>
            <h1>Send from your own address</h1>
            <p className="muted" style={{ fontSize: 15, maxWidth: 520 }}>
              Connect a mailbox once. Every <b style={{ color: "#fff" }}>Send email</b> step uses it, so customers get mail from you,
              not from a no-reply robot.
            </p>
          </div>
          <div className="stack" style={{ gap: 10 }}>
            {["Turn on 2-step verification for your mail account", "Create an app password (a 16-character code)", "Paste it below. We send a test email before saving"].map((t, i) => (
              <div key={t} className="row" style={{ gap: 12 }}>
                <span className="mini-step trigger" style={{ fontWeight: 600, fontSize: 12 }}>{i + 1}</span>
                <span>{t}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      <div className="grid-3">
        <div className="card">
          <div className="card-head"><h3>Connect a mailbox</h3><span className="chip"><IconLock size={12} /> stored encrypted</span></div>
          <div className="card-body stack" style={{ gap: 16 }}>
            <div className="segmented" style={{ alignSelf: "flex-start", flexWrap: "wrap" }}>
              {PROVIDERS.map((x) => (
                <button key={x.id} className={provider === x.id ? "active" : ""} onClick={() => { setProvider(x.id); setErr(null); }}>{x.name}</button>
              ))}
            </div>
            <div className="grid-2" style={{ gap: 12 }}>
              <label className="field">Email address<input placeholder={p.hint} value={form.from_email} onChange={(e) => set("from_email", e.target.value)} /></label>
              <label className="field">Sender name (optional)<input placeholder="Acme Support" value={form.from_name} onChange={(e) => set("from_name", e.target.value)} /></label>
            </div>
            {provider === "custom" && (
              <div className="row" style={{ alignItems: "flex-end" }}>
                <label className="field" style={{ flex: 2 }}>SMTP host<input placeholder="smtp.yourdomain.com" value={form.smtp_host} onChange={(e) => set("smtp_host", e.target.value)} /></label>
                <label className="field" style={{ flex: 1 }}>Port<input value={form.smtp_port} onChange={(e) => set("smtp_port", e.target.value)} /></label>
                <label className="field">Security
                  <select value={form.security} onChange={(e) => set("security", e.target.value)}>
                    <option value="starttls">STARTTLS</option><option value="ssl">SSL/TLS</option><option value="none">None</option>
                  </select>
                </label>
              </div>
            )}
            <label className="field">
              {provider === "custom" ? "Password" : "App password"}
              <input type="password" placeholder={provider === "gmail" ? "abcd efgh ijkl mnop" : "••••••••"} value={form.password} onChange={(e) => set("password", e.target.value)} />
            </label>
            {presets?.[provider] && (
              <div className="muted small">
                {presets[provider].help}{" "}
                {p.link && <a href={p.link} target="_blank" rel="noreferrer" style={{ color: "var(--green-strong)", fontWeight: 600 }}>Open settings →</a>}
              </div>
            )}
            <div className="row">
              <button className="btn primary" disabled={busy || !form.from_email} onClick={connect}>
                <IconMail size={14} /> {busy ? "Verifying with your mail server…" : "Connect & send test email"}
              </button>
            </div>
            {err && <div className="err-box">{err}</div>}
          </div>
        </div>

        <div className="card">
          <div className="card-head"><h3>Connected</h3><span className="muted small">{accounts?.length ?? 0}</span></div>
          {accounts && !accounts.length ? (
            <Empty title="No mailbox yet"><p>Send email steps fail until one is connected.</p></Empty>
          ) : (
            accounts?.map((a) => (
              <div key={a.id} className="mail">
                <div className="mail-icon"><IconMail size={16} /></div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <span className="subject" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{a.from_email}</span>
                    {a.is_default && <StatusPill status="completed" label="Default" />}
                  </div>
                  <div className="muted small">{a.from_name ? `${a.from_name} · ` : ""}{a.smtp_host}:{a.smtp_port} · verified {ago(a.verified_at)}</div>
                  <div className="row" style={{ marginTop: 8 }}>
                    <button className="btn sm" onClick={async () => {
                      try { const r = await api.testEmail(a.id); toast(`Test email sent to ${r.sent_to}`); } catch (e) { toast((e as Error).message); }
                    }}>Send test</button>
                    <button className="btn sm ghost danger" onClick={async () => {
                      if (!confirm(`Remove ${a.from_email}?`)) return;
                      await api.deleteEmail(a.id);
                      reload();
                    }}><IconTrash size={13} /> Remove</button>
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

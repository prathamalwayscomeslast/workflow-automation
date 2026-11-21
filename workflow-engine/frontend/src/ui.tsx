import type { ReactNode } from "react";
import type { StepDef } from "./api";
import { copy, statusOf } from "./lib";

// --------------------------------------------------------------------------- icons (inline SVG, stroke = currentColor)

type IconProps = { size?: number; className?: string };
const svg = (path: ReactNode) =>
  function Icon({ size = 16, className }: IconProps) {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
        {path}
      </svg>
    );
  };

export const IconCheck = svg(<path d="M20 6 9 17l-5-5" />);
export const IconX = svg(<path d="M18 6 6 18M6 6l12 12" />);
export const IconClock = svg(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>);
export const IconBolt = svg(<path d="M13 2 4 14h7l-1 8 9-12h-7z" />);
export const IconMail = svg(<><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></>);
export const IconGear = svg(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></>);
export const IconBranch = svg(<><circle cx="6" cy="5" r="2" /><circle cx="6" cy="19" r="2" /><circle cx="18" cy="9" r="2" /><path d="M6 7v10M18 11c0 4-6 3-12 6" /></>);
export const IconHourglass = svg(<path d="M6 2h12M6 22h12M7 2c0 6 10 6 10 10S7 16 7 22M17 2c0 6-10 6-10 10s10 4 10 10" />);
export const IconCopy = svg(<><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></>);
export const IconPlus = svg(<path d="M12 5v14M5 12h14" />);
export const IconChevron = svg(<path d="m9 6 6 6-6 6" />);
export const IconPlay = svg(<path d="m7 4 13 8-13 8z" />);
export const IconRefresh = svg(<><path d="M21 12a9 9 0 1 1-3-6.7L21 8" /><path d="M21 3v5h-5" /></>);
export const IconTrash = svg(<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14" />);
export const IconChat = svg(<path d="M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12z" />);
export const IconCircle = svg(<circle cx="12" cy="12" r="8" />);
export const IconSpark = svg(<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8" />);
export const IconLock = svg(<><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></>);

export function Spinner({ size = 16, className = "" }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className={`spin ${className}`} aria-hidden>
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity=".25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function StepIcon({ type, size = 15 }: { type: StepDef["type"] | "trigger"; size?: number }) {
  switch (type) {
    case "trigger": return <IconBolt size={size} />;
    case "send_email": return <IconMail size={size} />;
    case "send_message": return <IconChat size={size} />;
    case "wait": return <IconHourglass size={size} />;
    case "branch": return <IconBranch size={size} />;
    default: return <IconGear size={size} />;
  }
}

/** Filled status glyph: green check, red x, blue spinner, amber clock. */
export function StatusIcon({ status, size = 18 }: { status: string; size?: number }) {
  const cls = `si ${status}`;
  if (status === "running" || status === "pending") return <Spinner size={size} className={cls} />;
  const bg: Record<string, string> = { completed: "var(--green-strong)", sent: "var(--green-strong)", failed: "var(--red)", waiting: "var(--amber)" };
  const color = bg[status];
  if (!color) return <IconCircle size={size} className={cls} />;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className={cls} aria-hidden>
      <circle cx="12" cy="12" r="11" fill={color} />
      {(status === "completed" || status === "sent") && <path d="m7 12.5 3.2 3.2L17 9" stroke="#fff" strokeWidth="2.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />}
      {status === "failed" && <path d="M8.5 8.5l7 7M15.5 8.5l-7 7" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" />}
      {status === "waiting" && <path d="M12 7v5l3 2" stroke="#fff" strokeWidth="2.2" fill="none" strokeLinecap="round" />}
    </svg>
  );
}

export function StatusPill({ status, label }: { status: string; label?: string }) {
  return (
    <span className={`status ${status}`}>
      <StatusIcon status={status} size={14} />
      {label ?? statusOf(status)}
    </span>
  );
}

export function Terminal({ text, prompt = "$" }: { text: string; prompt?: string }) {
  return (
    <div className="terminal">
      <span className="prompt">{prompt}</span>
      <code title={text}>{text}</code>
      <button className="btn dark sm icon" onClick={() => copy(text)} title="Copy">
        <IconCopy size={14} />
      </button>
    </div>
  );
}

/** Syntax-tinted JSON for the console blocks. */
export function JsonView({ value }: { value: unknown }) {
  const json = JSON.stringify(value, null, 2) ?? "null";
  const parts = json.split(/("(?:\\.|[^"\\])*"(?:\s*:)?|\b-?\d+(?:\.\d+)?\b|\btrue\b|\bfalse\b|\bnull\b)/g);
  return (
    <pre className="console">
      {parts.map((p, i) =>
        /^".*":$/.test(p.replace(/\s/g, "")) ? <span key={i} className="k">{p}</span>
        : p.startsWith('"') ? <span key={i} className="s">{p}</span>
        : /^(-?\d|true|false|null)/.test(p) ? <span key={i} className="n">{p}</span>
        : p,
      )}
    </pre>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="mini-step trigger" style={{ width: 40, height: 40, borderRadius: 12 }}><IconBolt size={20} /></div>
      <div style={{ color: "var(--ink)", fontWeight: 600 }}>{title}</div>
      {children}
    </div>
  );
}

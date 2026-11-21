import { useCallback, useEffect, useRef, useState } from "react";
import type { Json, RunStatus, StepDef } from "./api";

// --------------------------------------------------------------------------- hooks

/** Fetch now and every `ms`; returns [data, reload, error]. */
export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = []): [T | undefined, () => void, string | null] {
  const [data, setData] = useState<T>();
  const [err, setErr] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const load = useCallback(() => {
    fnRef.current().then(
      (d) => {
        setData(d);
        setErr(null);
      },
      (e: Error) => setErr(e.message),
    );
  }, []);
  useEffect(() => {
    setData(undefined);
    load();
    const t = setInterval(load, ms);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return [data, load, err];
}

/** Tiny hash router: "#/w/3/r/9" -> ["w","3","r","9"]. */
export function useRoute(): string[] {
  const parse = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const [parts, setParts] = useState<string[]>(parse);
  useEffect(() => {
    const on = () => {
      setParts(parse());
      window.scrollTo({ top: 0 });
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return parts;
}

export const go = (path: string) => {
  window.location.hash = path;
};

/** Re-render every `ms` (for countdowns / "x ago"). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

// --------------------------------------------------------------------------- toast

export function toast(msg: string) {
  window.dispatchEvent(new CustomEvent("toast", { detail: msg }));
}

export function useToast(): string | null {
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    let t: ReturnType<typeof setTimeout>;
    const on = (e: Event) => {
      setMsg((e as CustomEvent<string>).detail);
      clearTimeout(t);
      t = setTimeout(() => setMsg(null), 2600);
    };
    window.addEventListener("toast", on);
    return () => window.removeEventListener("toast", on);
  }, []);
  return msg;
}

export async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard");
  } catch {
    toast("Copy failed. Select the text manually.");
  }
}

// --------------------------------------------------------------------------- formatting

export function ago(iso: string | null, now = Date.now()): string {
  if (!iso) return "—";
  const s = Math.round((now - new Date(iso).getTime()) / 1000);
  const abs = Math.abs(s);
  const txt = abs < 45 ? `${abs}s` : abs < 3600 ? `${Math.round(abs / 60)}m` : abs < 86400 ? `${Math.round(abs / 3600)}h` : `${Math.round(abs / 86400)}d`;
  return s >= 0 ? `${txt} ago` : `in ${txt}`;
}

export function duration(ms: number): string {
  if (!isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function between(a: string | null, b: string | null, now = Date.now()): string {
  if (!a) return "—";
  return duration((b ? new Date(b).getTime() : now) - new Date(a).getTime());
}

export const STATUS_LABEL: Record<string, string> = {
  completed: "Success",
  failed: "Failed",
  running: "Running",
  pending: "Queued",
  waiting: "Waiting",
  skipped: "Skipped",
  sent: "Sent",
  idle: "Not run",
  disabled: "Disabled",
};

export function statusOf(s: RunStatus | string): string {
  return STATUS_LABEL[s] ?? s;
}

const OPS: Record<string, string> = { eq: "=", ne: "≠", gt: ">", gte: "≥", lt: "<", lte: "≤", in: "in", not_in: "not in", contains: "contains", exists: "exists", not_exists: "is missing" };

export function condText(c: Json | undefined | null): string {
  if (!c) return "";
  if ("all" in c) return (c.all as Json[]).map(condText).join(" and ");
  if ("any" in c) return (c.any as Json[]).map(condText).join(" or ");
  if ("not" in c) return `not (${condText(c.not as Json)})`;
  const field = String(c.field).replace(/^trigger\./, "");
  const op = OPS[String(c.op)] ?? String(c.op);
  return c.value === undefined || c.value === null ? `${field} ${op}` : `${field} ${op} ${JSON.stringify(c.value)}`;
}

export const STEP_LABEL: Record<StepDef["type"], string> = {
  task: "Task",
  wait: "Wait",
  branch: "If / else",
  send_message: "Message",
  send_email: "Send email",
};

export function stepTitle(s: StepDef): string {
  if (s.name) return s.name;
  switch (s.type) {
    case "send_email":
      return s.subject ? `Email: ${s.subject}` : "Send email";
    case "wait":
      return `Wait ${s.duration}`;
    case "task":
      return s.task === "http" ? "HTTP request" : `Run ${s.task}`;
    case "branch":
      return "If / else";
    default:
      return s.id;
  }
}

export function stepDesc(s: StepDef): string {
  switch (s.type) {
    case "task":
      if (s.task === "http") return `${String(s.params?.method ?? "GET")} ${String(s.params?.url ?? "")}`;
      return s.params && Object.keys(s.params).length ? JSON.stringify(s.params) : `built-in task "${s.task}"`;
    case "wait":
      return `Pause the run for ${s.duration}`;
    case "branch":
      return `if ${condText(s.condition)}`;
    case "send_message":
      return `${s.channel} → ${s.to}`;
    case "send_email":
      return `to ${s.to}`;
  }
}

export function samplePayload(event: string): Json {
  if (event.startsWith("order") || event === "webhook")
    return { order_id: `o-${Math.floor(Math.random() * 9000 + 1000)}`, amount: 750, email: "customer@example.com", name: "Ada" };
  return { email: "customer@example.com", name: "Ada", plan: "pro" };
}

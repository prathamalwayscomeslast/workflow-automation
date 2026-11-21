export type Json = Record<string, unknown>;

export interface Workflow {
  id: number;
  name: string;
  description: string | null;
  trigger: { event: string; conditions?: Json };
  steps: StepDef[];
  source: "json" | "text" | "yaml";
  source_text: string | null;
  enabled: boolean;
  created_at: string;
  webhook_url: string | null;
  webhook_signed: boolean;
}

export interface EmailAccount {
  id: number;
  label: string;
  from_email: string;
  from_name: string | null;
  smtp_host: string;
  smtp_port: number;
  security: string;
  username: string | null;
  is_default: boolean;
  verified_at: string | null;
  created_at: string;
}

export interface StepDef {
  id: string;
  type: "task" | "wait" | "branch" | "send_message" | "send_email";
  name?: string;
  next?: string;
  task?: string;
  params?: Json;
  duration?: string | number;
  condition?: Json;
  then?: string;
  else?: string;
  channel?: string;
  to?: string;
  subject?: string;
  account_id?: number;
  body?: string;
}

export type RunStatus = "pending" | "running" | "waiting" | "completed" | "failed";

export interface Run {
  id: number;
  workflow_id: number;
  status: RunStatus;
  current_step: number | null;
  step_count: number;
  context: Json;
  wake_at: string | null;
  lease_owner: string | null;
  lease_expires_at: string | null;
  claim_count: number;
  error: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface RunStep {
  seq: number;
  step_index: number;
  step_id: string;
  step_type: string;
  status: string;
  output: Json | null;
  error: string | null;
  worker_id: string | null;
  started_at: string;
  finished_at: string;
}

export interface RunDetail extends Run {
  steps: RunStep[];
  workflow_steps: StepDef[];
}

export interface Message {
  idempotency_key: string;
  step_id: string;
  channel: string;
  to: string;
  payload: { body?: string; subject?: string };
  outbox_status: string;
  attempts: number;
  delivered: boolean;
  provider_attempts: number;
  sent_at: string | null;
}

export interface Stats {
  workflows: number;
  runs: Partial<Record<RunStatus, number>>;
  outbox: Record<string, number>;
  sink_messages: number;
  sink_duplicate_attempts_absorbed: number;
}

export interface SpecPreview {
  name: string;
  description?: string | null;
  trigger: Workflow["trigger"];
  steps: StepDef[];
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body;
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail, null, 2));
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const post = <T>(path: string, body: unknown) => req<T>(path, { method: "POST", body: JSON.stringify(body) });

export const api = {
  health: () => req<{ ok: boolean; nl_generator: string; wait_time_scale: number; tasks: string[] }>("/health"),
  stats: () => req<Stats>("/stats"),
  workflows: () => req<Workflow[]>("/workflows"),
  createWorkflow: (spec: unknown) => post<Workflow>("/workflows", spec),
  validate: (spec: unknown) => post<{ valid: boolean; spec?: SpecPreview; errors?: Json[] }>("/workflows/validate", spec),
  fromText: (text: string, save: boolean) =>
    post<{ workflow: Workflow | null; spec: SpecPreview; generator: string; notes: string[] }>("/workflows/from-text", { text, save }),
  fromYaml: (yaml: string, save: boolean) =>
    post<{ workflow: Workflow | null; spec: SpecPreview }>("/workflows/from-yaml", { yaml, save }),
  deleteWorkflow: (id: number) => req<void>(`/workflows/${id}`, { method: "DELETE" }),
  setEnabled: (id: number, enabled: boolean) =>
    req<Workflow>(`/workflows/${id}`, { method: "PATCH", body: JSON.stringify({ enabled }) }),
  sendEvent: (event: string, payload: Json, idempotency_key?: string) =>
    post<{ matched_workflows: number; run_ids: number[]; skipped: Json[] }>("/events", {
      event,
      payload,
      idempotency_key: idempotency_key || undefined,
    }),
  emailAccounts: () => req<EmailAccount[]>("/email-accounts"),
  emailPresets: () => req<Record<string, { help: string }>>("/email-accounts/presets"),
  connectEmail: (body: Json) => post<EmailAccount>("/email-accounts", body),
  testEmail: (id: number) => post<{ ok: boolean; sent_to: string }>(`/email-accounts/${id}/test`, {}),
  deleteEmail: (id: number) => req<void>(`/email-accounts/${id}`, { method: "DELETE" }),
  webhookSettings: (id: number, body: { rotate_token?: boolean; secret?: string }) =>
    post<Workflow>(`/workflows/${id}/webhook`, body),
  /** Calls a workflow's webhook through the dev proxy, exactly as an external service would. */
  callHook: (webhookUrl: string, payload: Json, idempotencyKey?: string) =>
    req<{ started: boolean; run_id?: number; reason?: string }>(`/hooks/${webhookUrl.split("/hooks/")[1]}`, {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json", ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}) },
    }),
  runs: (workflowId?: number) => req<Run[]>(`/runs?limit=100${workflowId ? `&workflow_id=${workflowId}` : ""}`),
  run: (id: number) => req<RunDetail>(`/runs/${id}`),
  messages: (id: number) => req<Message[]>(`/runs/${id}/messages`),
};

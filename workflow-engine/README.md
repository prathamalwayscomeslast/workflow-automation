# Workflow Automation Engine

A durable engine for customer automations such as *"When a new order arrives, if it is over
$500, wait one day, then send a follow-up message."*

Think "n8n, but much smaller". A workflow is a **trigger** and a **chain of steps**:
- **When this happens:** a **webhook**. Every workflow gets its own URL, and anything POSTed
  there starts a run (optional conditions, HMAC signing, idempotency key). Internal events via
  `POST /events` also work.
- **Do this:** **send an email from your own mailbox** (Gmail/Outlook/Yahoo/Zoho/any SMTP,
  connected with an app password), call an HTTP API, wait, or branch on a condition.

You can write workflows as JSON, as a GitHub-Actions-style **YAML pipeline**, or in **plain
English**.

The core property: **all run state lives in Postgres, not in process memory.** If you kill the
engine at any moment and restart it, every run continues from where it stopped, and no message
is delivered twice. A test proves this with a real `SIGKILL`
([`backend/tests/test_recovery.py`](backend/tests/test_recovery.py)).

**Stack:** Python 3.11+ · FastAPI · SQLAlchemy 2 + psycopg 3 · Postgres (Neon) · React + TypeScript + Vite.

---

## Design note: running 50,000 workflows at once

- **Postgres is the queue. Claiming uses `SELECT … FOR UPDATE SKIP LOCKED` on a `(status, wake_at)` index.**
  Due work is a cheap index range scan: `pending` runs, plus `waiting` runs whose `wake_at <= now()`.
  Any number of workers claim in parallel without blocking each other or double-claiming, and a
  lease (`lease_owner`, `lease_expires_at`) marks who owns a run. 50k *active* runs is a small
  table for Postgres. The real limit is claim/commit throughput, so the next steps are batched
  claims (`LIMIT 100`) and a partial index on due rows.
- **Workers are stateless and scale horizontally.** A worker is "claim → run one step → commit"
  in a loop. Add processes or machines to add throughput. Recovery needs no coordinator: an
  expired lease is just another kind of due work, so any surviving worker picks up a dead
  worker's runs within `LEASE_SECONDS`.
- **Waits are rows, not coroutines.** A `wait` step sets `status=waiting, wake_at=…` and frees
  the worker at once. 50,000 runs that each wait a day cost 50,000 rows and **zero** threads,
  timers, or memory, and they survive deploys and crashes. (The old design slept in-process
  with `asyncio.sleep`, so a restart lost every pending wait.)
- **Messages go through a transactional outbox with an idempotency key.** The outbox row
  (`run_id:step_id:seq`, `UNIQUE`) is inserted *in the same transaction* that records the step.
  A separate dispatcher delivers it and passes the key to the provider. Retries are always
  safe: re-enqueue is a no-op, and re-delivery is deduped by the provider.
- **Slow or external work moves onto queues.** In this build, HTTP tasks run inline on the worker
  thread with a capped timeout. At scale, the engine would publish task jobs to a queue
  (SQS/Kafka/Redis streams) served by separate task-worker pools with per-tenant concurrency
  limits and rate limiting. The orchestrator then only does cheap state transitions, and one
  slow customer webhook can't starve everyone else.
- **Partition and shard for growth.** Partition `runs`/`run_steps` by time (or hash of `run_id`)
  so completed history is cheap to archive or drop, and the hot "due" index stays small.
  Beyond one primary, shard by `tenant_id`/`run_id` across databases, with each worker pool
  bound to a shard. Neon read replicas serve the UI and audit queries without touching the
  write path.

---

## Guarantees (and what they really mean)

| Concern | How it's handled |
|---|---|
| Engine killed mid-wait | The wait is a row (`wake_at`). After restart it wakes on time and continues. |
| Engine killed mid-step | The step's result wasn't committed, so after the lease expires another worker re-runs **that step only**. Earlier steps are never redone. |
| Two workers on one run | `SKIP LOCKED` claim plus a **fenced commit**: `UPDATE … WHERE lease_owner = me AND step_count = seq`. A worker that lost its lease can't commit. `UNIQUE(run_id, seq)` on `run_steps` is a second safety net. |
| Duplicate messages | Outbox `idempotency_key UNIQUE` makes enqueue idempotent. The provider dedupes deliveries by the same key. |
| Duplicate events | Optional `idempotency_key` on `POST /events`, enforced by `UNIQUE(workflow_id, trigger_key)`. |

**Honest wording:** step execution is **at-least-once**. Exactly-once *side effects* against an
external system are impossible in general. If the process dies after the provider accepted a
message but before we recorded that, we must re-send. What we guarantee is **at-least-once
attempts + idempotent delivery = each message is delivered once**. The demo message provider is
a local "sink" table that dedupes by idempotency key and **counts attempts**, the way Stripe,
Twilio, or SES-with-dedupe behave. So the test can prove "attempted twice, delivered once"
instead of just claiming it. HTTP `task` steps send the same `Idempotency-Key` header, so
receivers can dedupe too.

---

## Architecture

```
 POST /events ──► match workflows + trigger conditions ──► INSERT runs (pending)   (no execution in request)
                                                              │
                ┌─────────────── worker process (N threads, stateless, any number of them) ───────────────┐
                │ claim:  UPDATE runs SET status='running', lease… WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)│
                │ loop:   load run → execute ONE step (pure handler) →                                      │
                │         ONE TX { fenced UPDATE runs (context, current_step, status/wake_at)                │
                │                  INSERT run_steps (audit)                                                  │
                │                  INSERT outbox ON CONFLICT DO NOTHING   (send_message only) }              │
                │         wait → status=waiting, wake_at=now()+d, release lease, move on                     │
                │ dispatcher: SELECT outbox pending FOR UPDATE SKIP LOCKED → provider.send(key) → mark sent  │
                └───────────────────────────────────────────────────────────────────────────────────────────┘
```

### Tables
| Table | Purpose |
|---|---|
| `workflows` | `name`, `trigger` JSONB (`event`, `conditions`), `steps` JSONB (validated), `source` (json/text/yaml) |
| `runs` | `status` (pending/running/waiting/completed/failed), `current_step`, `step_count`, `context` JSONB (trigger payload + step outputs), `wake_at`, `lease_owner`, `lease_expires_at`, `claim_count` |
| `run_steps` | One row per executed step: `seq`, status, output, error, worker, timestamps. Audit trail + UI timeline. `UNIQUE(run_id, seq)` |
| `outbox` | `idempotency_key UNIQUE`, payload, status (pending/sent), attempts |
| `sink_messages` | Demo provider: `idempotency_key UNIQUE`, `attempts` counter |

### Code map
```
backend/app/
  main.py            FastAPI app (+ in-process worker when RUN_WORKER=true)
  config.py, db.py   settings; engine tuned for Neon (pre-ping, long timeouts, no prepared stmts)
  models.py          tables above
  schemas.py         WorkflowSpec: the single Pydantic contract for JSON, YAML and NL
  nl.py              plain English → WorkflowSpec (Claude + rule-based fallback)
  yaml_compiler.py   GitHub-Actions-style YAML → WorkflowSpec
  engine/
    worker.py        executor threads + outbox dispatcher; `python -m app.engine.worker`
    executor.py      claim SQL, step loop, fenced commits
    steps.py         step-type + task registries (task, wait, branch, send_message; http/echo/set/…)
    conditions.py    eval-free conditions, {{templates}}, `if:` expression parser
    outbox.py        enqueue + delivery + providers (local sink, optional webhook)
    triggers.py      event intake
  routes/            workflows.py, runs.py
backend/tests/       test_recovery.py (SIGKILL + crash-window tests), test_unit.py
frontend/            React UI
```

---

## Running it

### 1. Backend
```powershell
cd workflow-engine/backend
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then set DATABASE_URL (Neon connection string)
uvicorn app.main:app --reload --port 8000
```
Tables are created on startup (`create_all`). Open http://localhost:8000/docs for the API.

`.env` options (see `.env.example`):

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | – | Postgres URL; `postgresql://` is rewritten to the psycopg driver |
| `ANTHROPIC_API_KEY` | empty | Enables Claude for plain-English workflows (rules fallback otherwise) |
| `RUN_WORKER` | `true` | Run worker threads inside the API process |
| `WORKER_CONCURRENCY` | `8` | Executor threads per process |
| `LEASE_SECONDS` | `30` | How long before a silent worker's runs are reclaimed |
| `WAIT_TIME_SCALE` | `1` | Multiplies every wait. `0.0000578` makes "1 day" ≈ 5 s for demos |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Base of the webhook URLs shown to users (set to your ngrok/public URL) |
| `ENCRYPTION_KEY` | auto | Fernet key for stored app passwords; generated into `backend/.secret_key` if empty |
| `OUTBOX_MAX_ATTEMPTS` | `6` | Email delivery attempts (exponential backoff) before marking `failed` |

Extra workers, from any machine:
```powershell
python -m app.engine.worker
```

### 2. Frontend
```powershell
cd workflow-engine/frontend
npm install
npm run dev                       # http://localhost:5173 (proxies /api → :8000)
```
The UI ("Relay") has: a dashboard (stats, workflow cards, recent runs), create from **Plain English / YAML / JSON** with a live pipeline preview, per-workflow webhook URL/signing settings, an Email accounts page, a test trigger that POSTs to the webhook, and a live run view (pipeline graph with step status, audit trail, email delivery status). Original description: trigger
events (single or many at once, optional idempotency key), a runs table with live polling, and
a run timeline (plan vs. executed steps, outputs, outbox → provider delivery status, wake-up
countdown, claim count).

### 3. Tests
```powershell
cd workflow-engine/backend
pytest tests/test_unit.py -q          # no DB needed
pytest tests/test_recovery.py -v -s   # needs DATABASE_URL; ~1-2 min against Neon
pytest tests/test_webhook_email.py -v # webhook → real SMTP email, using a local SMTP server
```
Stop any running API/worker using the same database before running `test_recovery.py`. Another
worker would happily (and correctly) steal the test's runs, which defeats the chaos test's
timing.

**What `test_recovery.py` proves**
1. `test_sigkill_mid_wait_resumes_without_duplicates`: starts a worker *process*, triggers 12
   runs (task → task → send → **wait 6 s** → branch → send), SIGKILLs the worker once at least
   half the runs are parked in their wait (others are mid-step), then restarts it. It asserts
   all 12 runs complete, there are exactly 24 outbox rows (all `sent`) and exactly 24 provider
   messages (one per key), every run's `run_steps` sequence has no gaps or duplicates, and the
   runs were actually resumed (`claim_count > 1`).
2. `test_crash_between_delivery_and_mark_sent`: fault injection (`CHAOS_EXIT_AFTER_DELIVERIES`)
   hard-exits the worker **after the provider accepted a message but before the outbox row is
   marked sent**, the worst window. After restart, that message is re-attempted and the provider
   dedupes it: `attempts == 2`, delivered messages == 1, all runs complete.

---

## Quick start: webhook → email in 3 calls

**1. Connect your mailbox.** For Gmail, turn on 2-Step Verification and create an app password
at https://myaccount.google.com/apppasswords. The account is only saved after a real login and
a test email succeed. The password is stored encrypted (Fernet; key in `ENCRYPTION_KEY` or the
auto-generated, gitignored `backend/.secret_key`).
```bash
curl -X POST localhost:8000/email-accounts -H "content-type: application/json" \
  -d '{"provider":"gmail","from_email":"you@gmail.com","from_name":"My Shop","password":"abcd efgh ijkl mnop"}'
```
Presets: `gmail`, `outlook`, `yahoo`, `zoho`, or `custom` (`smtp_host`, `smtp_port`,
`security`: ssl|starttls|none). `GET /email-accounts/presets` shows setup help for each. The
first account becomes the default.

**2. Create a workflow** (here in plain English; YAML/JSON work too):
```bash
curl -X POST localhost:8000/workflows/from-text -H "content-type: application/json" \
  -d '{"text":"When a webhook is received, if the amount is over $500, wait 10 seconds, then send a thank-you email."}'
```
The response includes `workflow.webhook_url`, for example `http://localhost:8000/hooks/Xy3…`.

**3. Call the webhook** from anything (Stripe, Shopify, a form, Zapier, curl):
```bash
curl -X POST http://localhost:8000/hooks/Xy3... -H "content-type: application/json" \
  -d '{"amount":750,"email":"customer@example.com","name":"Ada","order_id":"o-1"}'
# → 202 {"started": true, "run_id": 42}
```
The email arrives from *your* address. Watch it with `GET /runs/42` and `GET /runs/42/messages`.

### Webhooks
- `POST`, `PUT`, or `GET /hooks/{token}`. JSON, form-encoded, or raw bodies are accepted, and
  the body's fields become `trigger.*` (for GET, the query parameters do). Method, query and a
  few headers are available as `webhook.*`.
- Responds `202` immediately, and the run happens on the worker. If trigger conditions don't
  match, it returns `{"started": false, "reason": …}`.
- `Idempotency-Key` header: retries of the same delivery never start a second run.
- **Signing (optional):** `POST /workflows/{id}/webhook {"secret": "…"}`. Callers must then
  send `X-Signature-256: sha256=<hex HMAC-SHA256(secret, raw body)>`. GitHub's
  `X-Hub-Signature-256` is accepted too.
- `POST /workflows/{id}/webhook {"rotate_token": true}` issues a new URL; the old one stops
  working.
- To receive webhooks from the internet locally, expose port 8000 (e.g. `ngrok http 8000`) and
  set `PUBLIC_BASE_URL` so the displayed URLs are correct.

### Email (`send_email` step)
```json
{ "id": "thanks", "type": "send_email", "to": "{{trigger.email}}",
  "subject": "Order {{trigger.order_id}} received", "body": "Hi {{trigger.name}}, thanks!",
  "account_id": 1 }
```
- `account_id` is optional; the default account is used if it's left out. `cc` is supported.
  The body is plain text (an HTML version with line breaks is generated automatically).
- If the account doesn't exist, the step **fails** and the run shows a clear error.
- SMTP failures are retried with exponential backoff (10 s, 20 s, 40 s…, up to
  `OUTBOX_MAX_ATTEMPTS`), then the message is marked `failed` with the error. You can see this in
  `GET /runs/{id}/messages`.
- **Duplicates:** the email is queued in the outbox exactly once per step (idempotency key).
  SMTP itself has no idempotency key, so a duplicate is only possible if the process dies in the
  instant between "mail server accepted" and "marked sent". To soften even that case, every
  email carries a deterministic `Message-ID` derived from the outbox key.
- **Why SMTP + app password instead of "Sign in with Google":** it works today with every
  provider and needs no OAuth app. Gmail's send scope requires Google's app verification (until
  then, only 100 named test users). OAuth is a planned upgrade and won't change the step or the
  outbox.
- `send_message` (with no account) still exists as a demo channel that writes to the local sink
  table. The recovery tests use it.

---

## Defining workflows

### JSON (the canonical form)
```json
{
  "name": "High-value follow-up",
  "trigger": { "event": "order.created", "conditions": { "field": "amount", "op": "gt", "value": 500 } },
  "steps": [
    { "id": "score",  "type": "task", "task": "score_order", "params": { "amount": "{{trigger.amount}}" } },
    { "id": "pause",  "type": "wait", "duration": "1 day" },
    { "id": "gold?",  "type": "branch", "condition": { "field": "steps.score.tier", "op": "eq", "value": "gold" },
      "then": "vip", "else": "followup" },
    { "id": "vip",      "type": "send_message", "to": "{{trigger.email}}", "body": "VIP thanks!", "next": "end" },
    { "id": "followup", "type": "send_message", "to": "{{trigger.email}}", "body": "How was order {{trigger.order_id}}?" }
  ]
}
```
- **Conditions** are data, never `eval()`: `{field, op, value}` with ops `eq ne gt gte lt lte in
  not_in contains exists not_exists`, composable with `{"all": [...]}`, `{"any": [...]}`,
  `{"not": {...}}`. `field` is a dot path into the run context. A bare `amount` falls back to
  `trigger.amount`.
- **Templates** use `{{path}}`: `trigger.*`, `steps.<id>.*`, `vars.*`, `event`. A value that is
  just one placeholder keeps its type (numbers stay numbers).
- **Flow:** steps run in order. `branch` jumps to `then`/`else` (step id or `end`), and any step
  can set `next`.
- **Built-in tasks:** `http` (method/url/json/headers, sends `Idempotency-Key`), `echo`, `set`
  (writes to `vars`), `score_order` (demo), `simulate_work` (ms of real work), `fail`.
  New tasks are one decorated function in `engine/steps.py` (`@task("name")`).
- The graph is validated up front: unique ids, every `then/else/next` target exists.

### YAML pipelines (GitHub-Actions style)
`POST /workflows/from-yaml` compiles YAML into the same `WorkflowSpec`:
```yaml
name: High-value order pipeline
on:
  event: order.created
  if: ${{ amount > 500 }}          # or: amount > 500 && country == "IN"
jobs:                               # optional; jobs run sequentially in `needs` order
  score:
    steps:
      - id: score
        uses: score_order           # task name
        with: { amount: "${{ trigger.amount }}" }
  notify:
    needs: score
    steps:
      - wait: 1 day
      - if: steps.score.tier == "gold"    # step-level if: skipped when false
        send: { to: "{{trigger.email}}", body: "VIP thanks!" }
      - send: { body: "How was your order?" }
```
- `uses`/`with` → task, `wait:` → wait, `send:` → message, `if`+`then`/`else` → explicit branch.
- A step-level `if:` compiles into a branch that skips the step, matching GitHub semantics.
- `${{ expr }}` and `{{expr}}` both work. Expressions support `== != > >= < <= in contains`,
  `&&`, `||`, `!`, parsed by a small eval-free parser.
- There's no shell `run:`. Executing arbitrary customer shell commands is a sandboxing project
  of its own.

### Plain English
`POST /workflows/from-text {"text": "...", "save": true}`
- With `ANTHROPIC_API_KEY`: Claude (`claude-opus-5-5`, configurable with `ANTHROPIC_MODEL`)
  is asked for JSON constrained by the `WorkflowSpec` JSON schema (structured outputs). The
  result is validated with **the same Pydantic model the engine uses**. If it's invalid, the
  validation errors go back to the model for **one** retry, and if it's still invalid the
  request is rejected (422). Nothing unvalidated is ever stored.
- Without a key (or if the API is unreachable): a deterministic rule-based parser handles the
  common shapes (event nouns, "over/under $N", "wait N days", "send a follow-up/welcome
  email/text", "call https://…").
- The brief's example sentence produces: trigger `order.created` with `amount > 500` →
  `wait 1 day` → `send_message` (covered by `test_unit.py`).

### API
| Method | Path | |
|---|---|---|
| `GET/POST` | `/workflows` | list / create (JSON spec) |
| `POST` | `/workflows/validate` | validate without saving |
| `POST` | `/workflows/from-text` | plain English → workflow |
| `POST` | `/workflows/from-yaml` | YAML pipeline → workflow |
| `GET/PATCH/DELETE` | `/workflows/{id}` | get / enable-disable / delete |
| `POST` | `/workflows/{id}/run` | start a run manually (skips trigger conditions) |
| `POST/GET/PUT` | `/hooks/{token}` | **webhook trigger** for one workflow (202) |
| `POST` | `/workflows/{id}/webhook` | rotate URL / set or clear HMAC secret |
| `PUT` | `/workflows/{id}` | replace a workflow definition |
| `GET/POST` | `/email-accounts` | list / connect a mailbox (verified with a test email) |
| `POST` | `/email-accounts/{id}/test`, `/email-accounts/{id}/default` | resend a test email / make default |
| `DELETE` | `/email-accounts/{id}` | remove a mailbox |
| `POST` | `/events` | `{event, payload, idempotency_key?}` → starts matching runs (202) |
| `GET` | `/runs`, `/runs/{id}`, `/runs/{id}/messages` | runs, timeline (run_steps), outbox + delivery |
| `GET` | `/stats`, `/health` | counters; config |

Example:
```bash
curl -X POST localhost:8000/events -H "content-type: application/json" \
  -d '{"event":"order.created","payload":{"order_id":"o-1","amount":750,"email":"ada@example.com"}}'
```

---

## Decisions and trade-offs
- **Postgres as the queue** (instead of Redis/Kafka/Temporal): one system to run, transactional
  with the state it schedules, and it scales well past this brief's needs. The design note covers
  when to add a real queue.
- **Polling (~1 s) instead of LISTEN/NOTIFY:** Neon's pooler (PgBouncer, transaction mode)
  doesn't support session `LISTEN`. Polling an indexed due-query is cheap and robust. Prepared
  statements are off for the same pooler reason, and `pool_pre_ping` plus a 30 s connect
  timeout absorb Neon's cold starts.
- **Threads with sync SQLAlchemy:** the work is I/O against Postgres, and threads keep the
  executor simple and debuggable. Horizontal scale comes from more processes, not async tricks.
- **Each step commits separately:** more round-trips, but a crash never loses more than the
  in-flight step. A long chain of non-side-effect steps could later be committed in batches.
- **Step handlers are pure:** they return "output + next + wait + message to enqueue", and the
  executor persists all of it atomically. That separation is what makes recovery easy to
  reason about.
- **Lease length vs. long steps:** a step that runs longer than `LEASE_SECONDS` could be
  reclaimed and run a second time (the fence still stops a double *commit*). Tasks are capped
  (HTTP 30 s, `simulate_work` 10 s) under the 30 s lease. A heartbeat would remove the coupling.

## Next steps (out of scope for this build)
- **Gmail/Outlook OAuth** ("Sign in with Google") as an alternative to app passwords.
- **Auth & multi-tenancy:** API keys/OAuth, `tenant_id` on every table, row-level security,
  per-tenant quotas.
- **Visual canvas:** drag-and-drop editor producing the same `WorkflowSpec`.
- **Retry / backoff policies:** per-step `retry: {max, backoff}` (today a failed step fails the
  run), dead-letter view, manual "resume from step".
- **Parallel steps:** fan-out/fan-in (`parallel: [...]` + join), and parallel jobs in YAML.
- Lease heartbeats, batched claims, a partial index on due rows, metrics (claim latency, due
  backlog, step p95), Alembic migrations instead of `create_all`, workflow versioning (runs pinned
  to the version they started on).

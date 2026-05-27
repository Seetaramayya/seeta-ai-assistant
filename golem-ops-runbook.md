# Golem Self-Hosted Operations Runbook

Diagnostic runbook built from live debugging sessions on EKS + RDS + S3.
Each section is a real incident with exact commands, log excerpts, root cause, and fix.

---

## 1. NoActiveShards — All Agent Operations Fail

### Symptom

Every agent invocation (including `golem agent list`) hangs or returns an error.
Worker service logs show a flood of:

```
error="NoActiveShards"
```

### Diagnosis

**Step 1 — Check shard manager logs for the root cause:**

```bash
kubectl logs -n golem golem-shard-manager-<pod> --tail=50
```

Look for the sequence:

```
# Shard manager tried to assign shards to executor but gRPC was not ready yet
Executing shard assignments assignments=[10.0.x.x:9000 : [<0>..<1023>]]
# BrokenPipe or connect timeout
gRPC call failed: Status { code: ... message: "BrokenPipe" }
# After 5 failed health checks, executor is removed
The following pods were found to be unhealthy: {Pod { ip: 10.0.x.x, port: 9000 }}
Pod removed pod=10.0.x.x:9000
# Rebalance with zero active pods → all shards unassigned
Beginning rebalance...
Executing shard assignments assignments=[]
```

**Step 2 — Confirm all pods are otherwise healthy:**

```bash
kubectl get pods -n golem
```

**Step 3 — Check that the executor is actually running (it may be CrashLoopBackOff):**

```bash
kubectl logs -n golem golem-worker-executor-<pod> --tail=20
```

### Root Cause

The shard manager starts before the worker executor's gRPC port is ready.
The `assign_shards` call fails with BrokenPipe. After 5 failed health checks
the shard manager marks the executor unhealthy and removes it from the active set.
With no active executors, all 1024 virtual shards are unassigned → `NoActiveShards`.

### Fix

Restart the worker executor so it re-registers with the shard manager:

```bash
kubectl rollout restart deployment/golem-worker-executor -n golem
```

**Verify the restart worked** — look for this sequence in shard manager logs:

```
Pod added pod=10.0.x.x:9000
Beginning rebalance...
Executing shard assignments assignments=[10.0.x.x:9000 : [<0>..<1023>]]
op success op="assign_shards"
```

Then:

```bash
export $(grep -v '^#' .env | xargs) && golem -E self-hosted agent list
```

Should return the agent list within a few seconds.

---

## 2. TelegramAgent Stuck — Idle with Pending > 0

### Symptom

`golem agent list` shows the TelegramAgent with `Status: Idle` and `Pending: 2`
(or any non-zero number). Invoking any method on the agent hangs indefinitely.
This persists for many minutes even after shard assignment is healthy.

### Diagnosis

**Step 1 — Confirm pending count is not clearing:**

```bash
export $(grep -v '^#' .env | xargs) && golem -E self-hosted agent list
```

```
| TelegramAgent("main") | 0 | Idle | 2 | ...
```

**Step 2 — Check if agent responds to direct invocations:**

```bash
export $(grep -v '^#' .env | xargs) && timeout 15 golem -E self-hosted agent invoke \
  'TelegramAgent("main")' status
```

If this times out (exit code 124), the 2 pending invocations are blocking the queue.

**Step 3 — Confirm the executor is actually loading the agent (WASM fetch from S3):**

```bash
kubectl logs -n golem golem-worker-executor-<pod> --tail=100 | grep "invocation-loop\|TelegramPoller"
```

Healthy output looks like:

```
invocation-loop{agent_id=".../TelegramAgent("main")"}:
  retry{target="compiled_component" op="get"}: op success duration_ms=108
```

If you see this log line but no subsequent invocation execution logs, the invocation-loop
completed oplog replay but is NOT consuming the pending queue — this is the stuck state.

**Step 4 — Check worker service for stale component ID errors:**

```bash
kubectl logs -n golem golem-worker-service-<pod> --tail=30
```

If you see a flood of:

```
Worker not found: <OLD-COMPONENT-UUID>/TelegramAgent("main")
```

where the UUID differs from the current deployment's component ID, there is stale RDS data
from a previous deployment that is being retried endlessly.
The current component ID can be found in the executor's `invocation-loop` log entry.

**Step 5 — Check the router for the source of stale requests:**

```bash
kubectl logs -n golem golem-router-<pod> --tail=20
```

Look for external IPs hammering the old component UUID:

```
10.0.1.x - - "GET /v1/components/<OLD-UUID>/workers/TelegramAgent.../connect" 404
```

Requests from external IPs (not 10.x.x.x) are coming from outside the cluster
(e.g. a `golem agent stream` left running in another terminal, or Golem Cloud agents).

### Root Cause

The two pending invocations were `poll()` scheduled tasks created by a PREVIOUS agent
instance that was running before a restart cycle. When the worker executor restarted,
the scheduler loop had not yet dropped these stale scheduled entries because the agent
was not yet recreated — it was merely reloaded from the same oplog.

The executor loaded the agent (fetched WASM from S3), replayed the oplog, and set status
to `Idle`. But the pending queue still held the 2 old `poll()` invocations. The
invocation-loop did not process them because they were associated with the old agent
creation record in the database.

This is confirmed by the scheduler log after the agent is deleted and recreated:

```
Scheduler loop: Dropping stale scheduled invocation:
  target worker was deleted and recreated
  agent_id="<env>/<component>/TelegramAgent("main")"
```

### Fix

Delete and recreate the TelegramAgent to clear its oplog and pending queue,
then call `start()` to re-arm polling:

```bash
export $(grep -v '^#' .env | xargs)

# 1. Delete the stuck agent
golem -E self-hosted agent delete 'TelegramAgent("main")'

# 2. Recreate it (empty oplog)
golem -E self-hosted agent new 'TelegramAgent("main")'

# 3. Start polling
golem -E self-hosted agent invoke 'TelegramAgent("main")' start
```

Expected output from step 3:

```
[INVOKE  ] STARTED  start
[INFO    ] [] TelegramAgent started
[INVOKE  ] FINISHED start
- "Polling started"
```

**Verify polling is running:**

```bash
export $(grep -v '^#' .env | xargs) && timeout 15 golem -E self-hosted agent list
```

Should show `Status: Running` and `Pending: 0`.

**Stream live output to confirm message processing:**

```bash
export $(grep -v '^#' .env | xargs) && timeout 15 golem -E self-hosted agent stream \
  'TelegramAgent("main")'
```

Look for `poll()` invocations completing every ~5 seconds and `INFO` lines for any
Telegram messages received.

---

## 3. Shard Manager Health-Check Mechanics

Understanding the shard manager's health-check loop helps interpret its logs.

### How it works

1. On startup, the shard manager waits for worker executors to register via gRPC.
2. Each registered executor gets assigned a slice of 1024 virtual shards.
3. A background health-check loop calls gRPC `healthcheck` on every registered executor
   every 10 seconds.
4. After **5 consecutive failures**, the executor is removed from the active set.
5. A rebalance is triggered: shards are redistributed across remaining active executors.
6. If no executors remain, all shards are unassigned → `NoActiveShards`.

### Key log patterns

| Log message | Meaning |
|---|---|
| `Pod added pod=X:9000` | Executor registered |
| `Executing shard assignments assignments=[X:9000 : [<0>..<N>]]` | Shards assigned |
| `op success op="healtcheck"` | Executor is healthy |
| `op success op="assign_shards"` | Assignment acknowledged by executor |
| `The following pods were found to be unhealthy: {Pod { ip: X }}` | Executor failed 5 health checks |
| `Pod removed pod=X:9000` | Executor removed from active set |
| `Beginning rebalance... assignments=[]` | No active executors — shards unassigned |

### Relevant diagnostic commands

```bash
# Watch shard manager in real time (filter out noisy healthcheck lines)
kubectl logs -n golem golem-shard-manager-<pod> -f | grep -v healtcheck

# Check how many active executors the shard manager knows about
kubectl logs -n golem golem-shard-manager-<pod> --tail=50 | grep "Pod added\|Pod removed\|rebalance"
```

---

## 4. Stale RDS Data After Namespace Re-Creation

### Background

When you run `kubectl delete namespace golem` and re-deploy, the **K8S resources** are
deleted but the **RDS database is not**. The schemas `golem_registry_service`,
`golem_shard_manager`, `golem_worker_executor`, and `golem_worker_executor_indexed` survive.

The new deployment generates new component UUIDs (because a new `golem deploy` was run).
The old component UUIDs remain in the RDS tables. The worker service will continue trying
to execute pending invocations for those old UUIDs indefinitely, causing:

- Constant "Worker not found" warnings in the worker service logs
- Unnecessary load on the worker service and router

### How to identify

```bash
kubectl logs -n golem golem-worker-service-<pod> --tail=20 | grep "Worker not found"
```

If the UUID in the error doesn't match the UUID visible in the executor's
`invocation-loop` or `GetWorkersMetadata` logs, you have stale data.

### Fix options

**Option A — Accept the noise (short-term).**
The stale invocations will eventually exhaust their retry budget or the worker service
will detect the component no longer exists in the registry and stop retrying.

**Option B — Fresh database (clean slate).**
Drop and recreate the golem database schemas in RDS before re-deploying:

```sql
DROP SCHEMA IF EXISTS golem_registry_service CASCADE;
DROP SCHEMA IF EXISTS golem_shard_manager CASCADE;
DROP SCHEMA IF EXISTS golem_worker_executor CASCADE;
DROP SCHEMA IF EXISTS golem_worker_executor_indexed CASCADE;
```

Then redeploy. The services will run their migrations and create fresh schemas.

**Option C — Full reset via Golem CLI.**
```bash
export $(grep -v '^#' .env | xargs) && golem -E self-hosted deploy --yes --reset
```
`--reset` deletes all agents and the environment record, then redeploys from scratch.
This clears the registry but does NOT wipe the RDS oplog tables directly — combine with
Option B for a fully clean state.

---

## 5. Quick Health Check Script

Run this to get a one-shot status of the entire stack:

```bash
#!/usr/bin/env bash
export $(grep -v '^#' .env | xargs)

echo "=== Pod Status ==="
kubectl get pods -n golem

echo ""
echo "=== Shard Manager (last assign/remove events) ==="
kubectl logs -n golem $(kubectl get pods -n golem -l app=golem-shard-manager -o name | head -1) \
  --tail=100 | grep "Pod added\|Pod removed\|rebalance\|assign_shards" | tail -10

echo ""
echo "=== Worker Executor (last invocation) ==="
kubectl logs -n golem $(kubectl get pods -n golem -l app=golem-worker-executor -o name | head -1) \
  --tail=50 | grep -v "healtcheck\|GetWorkersMetadata\|GetAgentMetadata" | tail -10

echo ""
echo "=== Agent List ==="
timeout 15 golem -E self-hosted agent list
```

---

## 6. Component Storage Limit Exceeded (422)

### Symptom

`golem deploy` fails with HTTP 422:

```
error: Component Service - Error: 422 Unprocessable Entity,
  TotalComponentStorageBytes limit exceeded
```

### Root Cause

The component service tracks total storage by summing the `size` column in
`golem_registry_service.component_revisions` for **all rows** — including rows where
`deleted = true`. Uploading a new revision (~60 MB each) eventually crosses the 500 MB
limit even though only the latest revision is actively used.

**What does NOT work:**
- `DELETE /v1/components/{id}?current_revision=N` — returns 409 (only the latest revision can be deleted this way, and that breaks the environment record)
- `UPDATE component_revisions SET deleted = true` — the service still sums these rows
- Deleting S3 blobs alone — the DB `size` column is still counted

### Fix

**Step 1 — Identify old revisions in RDS:**

```bash
kubectl run -n golem psql-tmp --image=postgres:15 --restart=Never --rm -it \
  --command -- psql "host=<rds-host> dbname=golem user=golem password=<pass>"
```

```sql
SET search_path TO golem_registry_service;

-- Check current storage usage (column is revision_id, not revision)
SELECT component_id, revision_id, size, deleted
FROM component_revisions
ORDER BY component_id, revision_id;

-- Zero out size on all old revisions, keeping only the latest active one
UPDATE component_revisions SET size = 0
WHERE revision_id < (
  SELECT MAX(revision_id) FROM component_revisions WHERE deleted = false
);
```

**Step 2 — Delete old WASM blobs from S3 (optional, saves actual storage cost):**

```bash
# List blobs for a component (find env_id and component_id from RDS or golem component list)
aws s3 ls s3://ais-storage/golem/<env_id>/<component_id>/

# Delete old revisions (keep the latest)
aws s3 rm s3://ais-storage/golem/<env_id>/<component_id>/0.cwasm
aws s3 rm s3://ais-storage/golem/<env_id>/<component_id>/1.cwasm
# ... repeat for each old revision
```

**Step 3 — Retry the deploy:**

```bash
./deploy.py self-hosted
```

### Prevention

Each deploy adds ~60 MB. After ~8 deploys the limit is hit. Run the cleanup SQL every
5–6 deploys, or after any batch of rapid iteration where many revisions were created.

---

## 7. Stale UserAgent Revisions After Deploy

### Symptom

After `./deploy.py self-hosted`, one or more UserAgents remain at an old component
revision (often revision 0):

```
| UserAgent("8487659930") | 0 | Idle | 0 | ...
```

Agents at revision 0 with oplog entries cannot auto-update. Invoking them may produce
responses using old code.

### Root Cause

When an agent is first created, Golem always creates it at revision 0. If the agent
accumulates oplog entries at revision 0, `golem agent update` to a later revision fails
with an oplog incompatibility error:

```
Unexpected oplog entry: expected BeginRemoteWrite, got AgentInvocationFinished
```

The auto-update deployed by `golem deploy --update-agents auto` also fails for the
same reason.

### Fix

The `deploy.py` script handles this automatically via `fix_stale_user_agents()`:

1. List all UserAgents and find any with `componentRevision != targetRevision`
2. Delete the stale agent
3. Recreate it via REST API with an empty oplog
4. Run `golem agent update <name> automatic <revision> --await`

```bash
./deploy.py self-hosted
```

If an agent is stale after a deploy, simply re-run the deploy script. The fix runs at
the end of every deploy automatically.

**Race condition:** If a user sends a Telegram message between the delete and recreate
steps, the new agent starts at revision 0 again and will need another deploy-script run.
Run `./deploy.py self-hosted` after a period of low user activity to minimise this.

---

## 8. Summary Table — Problems, Causes, and Fixes

| Problem | Root Cause | Fix |
|---|---|---|
| `NoActiveShards` | Shard manager removed executor after 5 failed gRPC health checks (BrokenPipe on startup) | `kubectl rollout restart deployment/golem-worker-executor -n golem` |
| `TelegramAgent` stuck `Idle + Pending > 0` | Stale scheduled invocations in RDS from a previous agent instance; invocation-loop loaded agent but did not consume old pending queue | Delete and recreate agent, then call `start()` |
| "Worker not found" flood in worker-service logs | Stale RDS data from a previous deployment with a different component UUID | Accept noise short-term, or drop+recreate RDS schemas for clean state |
| `agent list` / `agent invoke` hangs forever | Agent has stuck pending invocations that block the queue | Same as TelegramAgent fix above — delete and recreate the agent |
| External IP hammering old component UUID | `golem agent stream` left running in another terminal (or Golem Cloud agent referencing old component) | Kill the stale `golem agent stream` process; update Cloud agent if needed |
| 422 TotalComponentStorageBytes exceeded | All component revision rows (including `deleted=true`) are summed; ~8 deploys × 60 MB hits 500 MB limit | Zero `size` in `component_revisions` for old rows; delete S3 blobs for old revisions |
| UserAgent stuck at old revision after deploy | Agents created at revision 0 accumulate oplog entries; `golem agent update` fails with oplog incompatibility | Re-run `./deploy.py self-hosted` — `fix_stale_user_agents` deletes, recreates via REST API, then updates |

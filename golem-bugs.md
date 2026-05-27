# Golem v1.5.1 Known Bugs

Bugs encountered on self-hosted Golem v1.5.1. Each entry has a reproduction recipe,
root cause, workaround, and whether it also affects Golem Cloud.

---

## Bug 1 — Agent Type Rename Breaks Component Upload (400)

### Summary

Renaming an agent type (Scala trait annotated with `@agentDefinition`) causes the next
`golem deploy` to fail with a 400. There is no way to recover without either a new
component name or direct database access.

### Steps to Reproduce

1. Have a deployed component with an agent type named e.g. `TelegramPollerAgent`.
2. Rename the Scala trait to `TelegramAgent` (update trait, impl, `golem.yaml`).
3. Run `golem deploy`. The CLI shows:

```
Planned changes:
  - create agent type TelegramAgent
  - delete agent type TelegramPollerAgent

error: Component Service - Error: 400 Bad Request,
  Agent type 'TelegramPollerAgent' is referenced in provision config
  but not declared in the component's agent types
```

### Root Cause

When uploading a new component revision, the Component Service validates the incoming
binary against the **previous revision's provision config** (the stored per-agent-type
env mappings). If the new binary declares fewer agent types than the old config (i.e.
you renamed or deleted an agent type), the upload is rejected.

This validation runs the wrong direction: it should allow an agent type to be removed
when the new binary explicitly no longer declares it.

### What Doesn't Work

- `golem deploy --reset` — clears agents and the environment record, but the provision
  config stays with the component. The same 400 occurs when the binary is re-uploaded.
- `golem deploy --redeploy-agents` — same issue; the staging step fails before any agents
  are touched.
- Deleting the component via REST API (`DELETE /v1/components/{id}?current_revision=N`)
  and redeploying — this deletes the binary but the environment record still references
  the deleted component ID, causing all subsequent deploys to fail with 404.

### Workaround

**Rename the component** in `golem.yaml`. A new component name creates a fresh
component with no provision config history.

```yaml
# Before (broken state)
components:
  seeta-ai-assistant:core:

# After (fresh start)
components:
  seeta-ai-assistant:app:
```

Also rename the SBT project to match (Golem CLI derives the SBT project name by
converting `seeta-ai-assistant:app` → `golem_claw_app`):

```scala
// seeta_ai_assistant.sbt
lazy val golem_claw_app = project
  .in(file(""))
.
..
golem.sbt.GolemPlugin.autoImport.golemBasePackage := Some("golem_claw_app")
```

Run `sbt golem_claw_app/clean` before the next deploy to clear the old build target.

### Affects Golem Cloud?

**Yes — and worse.** On Golem Cloud there is no database access, so the "delete
component, fix DB" recovery path is not available at all. If you hit this bug on
Golem Cloud, the only option is to rename the component as described above.

---

## Bug 2 — No Clean Path to Delete a Full Deployment

### Summary

There is no single CLI command or API call that fully removes a deployment (component +
environment + domain registration) in one shot. Partial deletes leave the system in a
broken state.

### The Gap

| What `--reset` deletes | What it does NOT delete          |
|------------------------|----------------------------------|
| All agents (workers)   | Component binary in the registry |
| Environment record     | Component provision configs      |
| Staged deployment      | Domain registration              |

`DELETE /v1/components/{id}?current_revision=N` removes the binary, but the environment
record still holds the now-missing component ID. Every subsequent `golem deploy` fails:

```
error: Component Service - Error: 404 Not Found,
  Component for id <uuid> not found
```

### Recovery (self-hosted only)

Connect to the RDS database from within the cluster and soft-delete the broken records:

```bash
# Run a postgres client inside the cluster
kubectl run -n golem psql-tmp --image=postgres:15 --restart=Never --rm -it \
  --command -- psql "host=<rds-host> dbname=golem user=golem password=<pass>"
```

```sql
SET search_path TO golem_registry_service;

-- Soft-delete the application so next deploy creates it fresh
UPDATE applications
SET deleted_at = now()
WHERE name = 'seeta-ai-assistant'
  AND deleted_at IS NULL;

-- Soft-delete the stale domain registration
UPDATE domain_registrations
SET deleted_at = now()
WHERE domain = 'golem-api.vadali.in'
  AND deleted_at IS NULL;
```

After that, `golem deploy` creates a new application, new environment, and new
component from scratch.

### Affects Golem Cloud?

**Yes — and unrecoverable.** No database access is available on Golem Cloud. If a
component is deleted via REST API on Cloud, the environment is permanently broken.
**Do not delete components via REST API.** Use `golem deploy --reset` to clean up
agents and environments, and leave the component in place.

---

## Bug 4 — Adding an Agent Call to an Existing Function Breaks Oplog Replay on Update

### Summary

If you add a new inter-agent call (e.g. a `MetricsAgentClient` call) to a function that
was already called at a previous component revision, upgrading existing agents to the new
revision causes oplog replay to fail with:

```
Unexpected oplog entry: expected golem::agent::get_agent_type, got io::poll::pollable::ready
```

The replaying agent panics. Any caller that awaits a result from it receives a
`remote-internal-error`, which Golem's ScalaJS runtime propagates as a raw JS object
`throw {…}` instead of a proper Scala exception. The caller panics with:

```
JavaScript exception: [object Object]
```

### Steps to Reproduce

1. Deploy rev N with `DirectoryAgentImpl.publishEmail` that calls only `putEmail`.
2. A user invokes `publishEmail` — this is recorded in the DirectoryAgent's oplog.
3. Add `MetricsAgentClient.get("global").recordContactRegistered()` to `publishEmail`.
4. Deploy rev N+1 (automatic update).
5. Call anything on DirectoryAgent. It replays the old `publishEmail` oplog: rev N+1 code
   emits `get_agent_type` for MetricsAgent first, but the oplog has the `putEmail` poll
   operations at that position → panic.
6. The caller gets `remote-internal-error` → JS object exception → caller panics.

### Root Cause

Golem's durable execution replays an agent's full oplog on each invocation (unless a
snapshot is present). Adding new side effects (agent calls, HTTP calls) to an existing
function changes the expected oplog structure, so replay fails.

Additionally, `remote-internal-error` results from inter-agent calls are not converted to
proper Scala exceptions by the ScalaJS runtime — they are thrown as raw JS objects, which
causes the caller to panic rather than handling the error gracefully.

### Prevention

**Keep storage agents free of cross-agent side effects.** Storage agents
(`DirectoryAgent`, `DirectoryShardAgent`) should not call other agents (e.g. MetricsAgent)
inside their mutations. Metrics reporting belongs in the caller (UserAgent). This is the
only design that avoids the problem entirely — the oplog for a pure storage agent only
records deterministic state mutations, which are safe across code changes.

**Add snapshotting for oplog compaction.** `snapshotting = "every(N)"` compacts the oplog
every N invocations. This reduces the vulnerable window (at most N–1 oplog entries can
be incompatible after a breaking change) but does NOT eliminate it — invocations recorded
since the last snapshot are still replayed with the new code.

### Migration Procedure for Breaking Changes

When you must add a new external call (agent call or HTTP) to an existing function, treat
it as a migration:

1. **Before deploying**, temporarily change the annotation to `every(1)` and deploy.
2. **Invoke the affected agent once** (any call will do) to force an immediate snapshot.
3. **Restore** the annotation to `every(N)` and deploy again with the breaking change.

After step 2 the agent has a snapshot of its current state with zero incompatible oplog
entries; step 3 replays from that clean snapshot.

If a migration wasn't done and agents are already broken:

### Recovery (data loss)

```bash
gs agent delete --yes 'DirectoryAgent("global")'
gs agent delete --yes 'UserAgent("6794831517")'
```

### Affects Golem Cloud?

**Yes.** The oplog replay mechanism is the same. The same `remote-internal-error` → JS
object panic chain applies.

---

## Bug 3 — Worker Service Does Not Reload Routes After New Deployment

### Summary

After a fresh deployment (new app or new component), the worker service keeps serving
routes from its in-memory cache of the previous (or absent) deployment. All HTTP API
requests return:

```json
{
  "code": "ROUTE_NOT_FOUND",
  "error": "Resolving route failed: No matching route for request"
}
```

### Steps to Reproduce

1. Have a broken deployment that you've recovered from (e.g. via DB soft-delete).
2. Run `golem deploy` successfully — new environment created.
3. Call any agent HTTP endpoint (e.g. `GET /telegram/main/status`).
4. Get `ROUTE_NOT_FOUND` immediately (elapsed_ms=0 in worker-service logs — no lookup attempted).

### Root Cause

The worker service loads compiled routes at startup and does not watch for deployment
changes at runtime. A new deployment pushed to the registry is not picked up until
the service is restarted.

### Workaround

Restart the worker service after a deployment that creates a new environment:

```bash
kubectl rollout restart deployment/golem-worker-service -n golem
kubectl rollout status deployment/golem-worker-service -n golem --timeout=60s
```

This only affects **new environments** (first deploy for an app, or after a DB
soft-delete recovery). Normal re-deploys to an existing environment update routes
correctly without a restart.

### Affects Golem Cloud?

Unknown — Golem Cloud does not expose pod-level operations. It may handle this
internally, or the issue may not occur because Cloud environments are not recreated
via DB surgery.

---

## Bug 5 — No Snapshot-Based Upgrade Path for Stale Agent Revisions

### Summary

There is no mechanism to upgrade a stale agent (e.g. stuck at revision 1) to the current
component revision (e.g. revision 20) while preserving its state. The only option today
is delete-and-recreate, which loses all oplog history and in-memory state.

### Steps to Reproduce

1. Agent `UserAgent("X")` is created at revision 0 and accumulates oplog entries.
2. Component is deployed many times — now at revision 20.
3. `golem agent update 'UserAgent("X")' automatic 20 --await` fails:
   ```
   Unexpected oplog entry: expected BeginRemoteWrite, got AgentInvocationFinished
   ```
4. No path exists to bring the agent to revision 20 while keeping its accumulated state.

### Root Cause

Golem's upgrade mechanism replays the existing oplog under the new code. If any oplog
entry is structurally incompatible with the new code (different call sequence, added
external call, etc.) replay panics. There is no facility to:

- Load a snapshot taken at revision N as the starting state
- Apply only the delta oplog entries since that snapshot under revision 20 code
- Promote the agent to revision 20 from that point forward

A snapshot-based upgrade would look like:

1. Force a snapshot of the current agent state at revision N.
2. Mark the agent's "oplog base" as that snapshot.
3. Re-enter the agent at revision 20 with zero pending oplog entries.

This would make the agent code-compatible with revision 20 without replaying any
historical entries recorded under old code.

### Workaround (data loss)

```bash
golem -E self-hosted agent delete 'UserAgent("X")'
golem -E self-hosted agent new 'UserAgent("X")'
```

All accumulated state (conversation history, facts, etc.) is lost.

### Prevention

Use the 3-step migration procedure from Bug 4 before any breaking deploy:

1. Temporarily set `snapshotting = "every(1)"` and deploy.
2. Invoke the agent once — forces an immediate snapshot with zero pending oplog.
3. Restore `snapshotting = "every(N)"`, apply breaking change, deploy.

After step 2 the agent holds its full state in a snapshot that is oplog-compatible
with revision N. After step 3 it continues from that snapshot under the new code.

This procedure must be run **before** the breaking deploy while the agent is healthy.
It cannot rescue an agent that is already broken.

### Affects Golem Cloud?

**Yes.** The upgrade mechanism is the same. On Cloud, delete-and-recreate is the only
recovery path and there is no database access to assist.

---

## Bug 6 — No Contract Migration for Oplog Replay (Missing Fields Panic Instead of Defaulting)

### Summary

When a function's parameter or return type gains a new field across revisions (e.g.
`case class Foo(a: String)` → `case class Foo(a: String, b: Option[String])`),
the agent panics during oplog replay because old oplog entries don't carry the new field.
The runtime provides no way to declare that a missing field should be treated as `None`
or any other default.

### Steps to Reproduce

1. Deploy rev N with `publishEmail(userId: String, email: String)`.
2. An agent invokes `publishEmail` — entry recorded in oplog.
3. Add a new optional parameter: `publishEmail(userId: String, email: String, label: Option[String])`.
4. Deploy rev N+1.
5. On next invocation the agent replays the oplog. The old entry has no `label` field.
   The WASM deserializer panics instead of substituting `None`.

### Root Cause

Golem serializes oplog entries as binary (CBOR or a similar format) tied to the exact
WIT type defined at the time of recording. The runtime has no schema registry or per-entry
version tag. When the type gains a field:

- The new deserializer expects the field.
- The old binary doesn't contain it.
- Result: deserialization error → panic → agent crash.

A proper solution would:

- Tag each oplog entry with the component revision it was recorded at.
- Allow developers to register migration functions: `(oldRevision, bytes) → newBytes`.
- Apply migrations lazily as entries are replayed.

Alternatively, the WIT type system could define "additive-only" evolution rules:
any new field in a record must be `option<T>` and absent entries replay with `none`.

### Workarounds

**1. Never change existing function signatures.**
Add new functions instead of modifying existing ones. Keep old functions in place
(even if unused) until all agents that could have oplog entries for them have been
deleted and recreated.

**2. Always use `Option[T]` for any field that might be absent.**
If a field needs to be added to an existing record type, make it `Option[T]` from day
one. This doesn't help retroactively — old oplog entries still don't carry the field —
but it at least makes the intent clear and forces callers to handle `None`.

**3. Force a snapshot before adding new fields (same as Bug 4 migration procedure).**
Before deploying the signature change:

1. Temporarily set `snapshotting = "every(1)"` and deploy (old signatures, no change).
2. Invoke each affected agent once — forces an immediate snapshot of the current state.
3. Restore `snapshotting = "every(N)"` and deploy the signature change.

After step 2 the agent's oplog base is the fresh snapshot. There are zero old-format
entries to replay; all future entries use the new format.

### Affects Golem Cloud?

**Yes.** The WIT serialization and oplog replay mechanism is identical. The same panic
occurs. Cloud offers no additional migration tooling.

---

## Golem Cloud Deployment Checklist

Steps to deploy to Golem Cloud once self-hosted is stable and tested. Differences
from self-hosted are marked.

### One-time Setup

1. Create a Golem Cloud account at `golem.cloud` and generate an API token.
2. Add the token to `.env`:
   ```
   GOLEM_CLOUD_TOKEN=<your-token>
   ```
3. In `deploy.py`, update the `golem-cloud` case:
   ```bash
   golem-cloud|cloud)
     ENV="cloud"
     GOLEM_SERVER_URL=""          # not needed; golem CLI uses cloud profile
     GOLEM_API_TOKEN="${GOLEM_CLOUD_TOKEN:-}"
     ;;
   ```
4. Set the cloud domain in `golem.yaml` (already set to `seeta-ai-assistant.apps.golem.cloud`).
5. Set `WEBHOOK_BASE_URL=https://seeta-ai-assistant.apps.golem.cloud` in `.env`.

### Deploy

```bash
./deploy.py golem-cloud
```

The deploy script builds, uploads the component, creates agents, and registers the
Telegram webhook automatically.

### Key Differences from Self-Hosted

| Topic                         | Self-Hosted                        | Golem Cloud                             |
|-------------------------------|------------------------------------|-----------------------------------------|
| Route reload after new env    | Requires `kubectl rollout restart` | Handled by Cloud (no action needed)     |
| Database access for recovery  | Available via `kubectl exec`       | Not available                           |
| Component rename bug recovery | DB surgery + rename                | Rename only                             |
| Domain                        | `golem-api.vadali.in` (custom)     | `seeta-ai-assistant.apps.golem.cloud` (managed) |
| TLS                           | Managed by K8S / Let's Encrypt     | Managed by Golem Cloud                  |
| Worker executor blob storage  | `emptyDir` (data lost on restart)  | Managed by Golem Cloud (persistent)     |
| Scaling                       | Manual K8S scaling                 | Managed by Golem Cloud                  |

### After Deploy — Verify

```bash
# Check agent status
source .env && golem -E cloud agent list

# Confirm webhook registered
curl -s https://seeta-ai-assistant.apps.golem.cloud/telegram/main/status

# Send a test message via Telegram
```

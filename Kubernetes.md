# Deploying Golem to Kubernetes

The `k8s/` directory contains 13 manifests (numbered `00`–`12`) converted from the Golem Docker Compose example (
`published-postgres`). They deploy all nine Golem services into a dedicated `golem` namespace.

---

## Clean up (or Delete everything)

One command removes all resources including PVCs (since volumes use `emptyDir` there is no persistent data to worry
about):

```bash
kubectl delete namespace golem
```

---

## Apply

`12-ingress.yaml` contains two placeholders that must be filled before applying:

- `YOUR_INGRESS_CLASS` — the name of nginx IngressClass
- `YOUR_DOMAIN` — your domain (e.g. `golem.example.com`)

### Step 1 — create the RDS secret

All manifests reference a K8S Secret named `golem-rds` for database credentials.
Create it from your `.env` file before applying anything else:

```bash
export $(grep -v '^#' .env | xargs)
kubectl create secret generic golem-rds -n golem \
  --from-literal=host=$RDS_HOST \
  --from-literal=database=$RDS_DB_NAME \
  --from-literal=username=$RDS_USERNAME \
  --from-literal=password=$RDS_PASSWORD \
  --from-literal=blob-bucket=$BLOB_STORAGE_BUCKET \
  --from-literal=blob-bucket-json=$BLOB_STORAGE_BUCKET_JSON
```

Note: the `golem` namespace must exist first (`00-namespace.yaml` applied) before the secret can be created.

### Step 2 — apply everything except the ingress

```bash
kubectl apply -f k8s/00-namespace.yaml \
              -f k8s/01-configmap.yaml \
              -f k8s/02-pvc.yaml \
              -f k8s/04-redis.yaml \
              -f k8s/05-registry-service.yaml \
              -f k8s/06-shard-manager.yaml \
              -f k8s/07-compilation-service.yaml \
              -f k8s/08-worker-executor.yaml \
              -f k8s/09-worker-service.yaml \
              -f k8s/10-debugging-service.yaml \
              -f k8s/11-router.yaml
```

### Step 3 — fill in the ingress values and apply

`k8s/ingress-values.local` contains actual values. Execute the following command with correct values:

```bash
# substitute YOUR_INGRESS_CLASS and YOUR_DOMAIN, then pipe to kubectl
sed -e 's/YOUR_INGRESS_CLASS/<your-ingress-class>/g' -e 's/YOUR_DOMAIN/<your-domain>/g' k8s/12-ingress.yaml | kubectl apply -f -
```

### Step 4 — wait for all pods to be Ready

```bash
kubectl get pods -n golem -w
```

All pods should reach `Running` state within a couple of minutes.

### Smoke test

```bash
curl http://<your-domain>/healthcheck   # should return {}
```

---

## Actual ingress values (local, git-ignored)

Environment-specific substitution values are in `k8s/ingress-values.local`.

---

## Notes

- Volumes use `emptyDir` — data does not survive pod restarts. Acceptable for a demo. For production install the EBS CSI driver and switch to `gp3` PVCs.
- The manifests target Golem image tag `v1.5.1`. Update all tags if you upgrade Golem.
- After pods are running, deploy GolemClaw with:
  ```bash
  export $(grep -v '^#' .env | xargs) && golem -e self-hosted deploy --yes
  ```

---

## Warning: Spot Instances

**Do not run Golem on spot instances with `emptyDir` volumes.** Two compounding problems:

**Problem 1 — emptyDir data is gone on eviction.**
When AWS reclaims a spot instance, all pods on it are killed and their `emptyDir` volumes
are wiped. This destroys:

- The PostgreSQL database (Golem's oplog — all durable execution state and agent data)
- The blob store (compiled WASM binaries)
- All running agent state

Golem's durability guarantees are built entirely on the oplog in PostgreSQL. Without a
persistent PostgreSQL, there are no durability guarantees.

**Problem 2 — EBS volumes are AZ-pinned.**
Even if you switch from `emptyDir` to proper EBS PVCs (`gp3`), an EBS volume lives in one
availability zone. If a spot instance in `eu-west-1a` is reclaimed and Kubernetes reschedules
the pod to `eu-west-1b`, the PVC cannot attach. The pod stays `Pending` until a node in the
original AZ becomes available.

### Production fix

Replace the two stateful components with managed AWS services:

| Component    | Manifests to remove                 | Replacement                            |
|--------------|-------------------------------------|----------------------------------------|
| PostgreSQL   | `03-postgres.yaml`                  | **RDS** (or Aurora Serverless)         |
| Blob storage | volume in `08-worker-executor.yaml` | **S3** bucket                          |
| Redis        | `04-redis.yaml`                     | **ElastiCache** (or keep on on-demand) |

Golem is designed for this — the Docker Compose example already externalises all three via
environment variables. Point the env vars at RDS/S3/ElastiCache instead of in-cluster pods.

The stateless Golem services (`worker-executor`, `worker-service`, `compilation-service`,
`shard-manager`, `registry-service`, `debugging-service`) have no local state and are safe
on spot.

### Short-term workaround (no RDS/S3)

Pin the stateful pods to on-demand nodes using a node label and `nodeSelector`:

```bash
# label your on-demand node(s)
kubectl label node <node-name> workload=on-demand
```

Add to `03-postgres.yaml` and `04-redis.yaml` under `spec.template.spec`:

```yaml
nodeSelector:
  workload: on-demand
```

This keeps postgres and Redis off spot nodes while allowing the stateless services to run
anywhere.

# Troubleshooting Guide

Welcome to the troubleshooting guide for the AlgoFleet Orchestrator. This document covers issues encountered during the project's development and operation, along with their solutions.

## Quick Reference

| # | Issue | Symptom | One-line Fix |
|---|---|---|---|
| 1 | CreateContainerConfigError on strategy pods | Pods failing at launch due to missing secret | Wait for External Secrets sync, verify with `kubectl get externalsecret` |
| 2 | Postgres pod in CrashLoopBackOff | Data directory conflict at `/var/lib/postgresql/data` | Set PGDATA env var to `/var/lib/postgresql/data/pgdata`, recreate StatefulSet |
| 3 | Bots recovering ghost trades | Bots trading on CockroachDB instead of Postgres | Inject `TRADE_DB_URL` into all deployment YAMLs pointing to internal Postgres |
| 4 | Wrong MT5 account | Bots trading on incorrect MT5 account (<INCORRECT_ACCOUNT_ID> vs <CORRECT_ACCOUNT_ID>) | Update secret in AWS Secrets Manager, restart pods |
| 5 | Prometheus/Grafana ArgoCD SyncFailed | CRD too large for `kubectl apply` | Use `kubectl apply --server-side` on unzipped chart CRDs |
| 6 | Grafana blank dark screen | Traffic not routed correctly via Classic Load Balancer | Add `nlb` annotation to Grafana Helm values |
| 7 | Grafana NLB resolving to internal IPs | DNS timeout due to internal-only NLB | Add `internet-facing` annotation to NLB service |
| 8 | StatefulSet immutable field error | Cannot update `volumeClaimTemplates` | Delete the existing StatefulSet and let ArgoCD recreate it |
| 9 | ArgoCD sync error | `--force` flag conflicting with `--server-side` | Trigger sync without the `--force` flag |

---

## Detailed Issues

### 1. CreateContainerConfigError on ALL strategy pods at launch
**Symptom**: All strategy bot pods entered a `CreateContainerConfigError` state immediately after creation.
**Root Cause**: The pods referenced a `secretKeyRef` that did not exist yet because the External Secrets Operator hadn't finished syncing `algofleet/engine-config` from AWS Secrets Manager.
**Commands to Diagnose**:
```bash
kubectl describe pod <bot-pod-name> -n trading
kubectl get externalsecret -n trading
```
**Commands to Fix**: No action required other than waiting for the sync to complete. If stuck, verify IAM permissions.
**Outcome**: Pods successfully started once the secret populated.
**Key Lesson Learned**: Kubernetes deployments referencing secrets will block pod creation until the secret is present. Expect a slight delay when relying on external secret sync.

### 2. Postgres pod in Error/CrashLoopBackOff
**Symptom**: The Postgres StatefulSet pod continually crashed.
**Root Cause**: PostgreSQL attempted to initialize the database cluster at `/var/lib/postgresql/data`. However, the root of the EBS volume was mounted at this path, and Postgres requires an empty directory.
**Commands to Diagnose**:
```bash
kubectl logs <postgres-pod> -n trading
```
**Commands to Fix**:
Update the deployment config to set the `PGDATA` environment variable to a subdirectory:
```yaml
env:
  - name: PGDATA
    value: /var/lib/postgresql/data/pgdata
```
Since StatefulSets have immutable fields, we had to delete it manually:
```bash
kubectl delete statefulset postgres -n trading
```
**Outcome**: ArgoCD recreated the StatefulSet and Postgres successfully initialized in the new subdirectory.
**Key Lesson Learned**: Always use a subdirectory for database data when mounting persistent volumes.

### 3. Strategy bots recovering ghost trades from wrong database
**Symptom**: Bots were logging trades that were clearly from a historical test dataset, completely ignoring the live Postgres database.
**Root Cause**: The underlying code (`trade_db.py`) had a hardcoded fallback `_HARDCODED_DB_URL` pointing to an old CockroachDB instance. Because the `TRADE_DB_URL` environment variable was missing from the deployment YAMLs, the bots silently fell back to the hardcoded test DB.
**Commands to Diagnose**:
Check pod environment variables:
```bash
kubectl exec -it <bot-pod> -n trading -- env | grep DB
```
**Commands to Fix**:
Inject the explicit DB URL into all strategy deployment manifests:
```yaml
env:
  - name: TRADE_DB_URL
    value: "postgresql://algofleet:supersecretpassword@postgres.trading.svc.cluster.local:5432/algofleet"
```
**Outcome**: Bots immediately connected to the internal Postgres database and functioned correctly.
**Key Lesson Learned**: Avoid hardcoded fallbacks in production code; enforce explicit configuration via environment variables.

### 4. Wrong MT5 account
**Symptom**: The bots were executing trades on MT5 account `<INCORRECT_ACCOUNT_ID>` instead of the expected `<CORRECT_ACCOUNT_ID>`.
**Root Cause**: The secret in AWS Secrets Manager contained an outdated `MT5_BRIDGE_URL`.
**Commands to Diagnose**:
Verify the injected secret contents (if possible/safe):
```bash
kubectl get secret engine-config -n trading -o jsonpath='{.data.json}' | base64 --decode
```
**Commands to Fix**:
Update the secret in AWS Secrets Manager (e.g., using a script):
```bash
./setup-secrets.sh
```
Then restart the deployments to pick up the new secret:
```bash
kubectl rollout restart deployment -n trading
```
**Outcome**: Pods restarted, picked up the refreshed secret from the External Secrets Operator, and connected to the correct account.
**Key Lesson Learned**: Changes in AWS Secrets Manager require pod restarts to take effect if not using dynamic reloading mechanisms.

### 5. Prometheus/Grafana ArgoCD SyncFailed
**Symptom**: ArgoCD reported a `SyncFailed` error when applying the `kube-prometheus-stack` Helm chart.
**Root Cause**: The Custom Resource Definitions (CRDs) for Prometheus exceeded the annotation size limit for a standard `kubectl apply`. ArgoCD failed to apply them. Additionally, raw GitHub URLs returned 404s.
**Commands to Fix**:
Manually pull and apply the CRDs using Server-Side Apply:
```bash
helm pull --untar prometheus-community/kube-prometheus-stack
kubectl apply --server-side -f kube-prometheus-stack/charts/crds/crds/
```
**Outcome**: CRDs were successfully applied, allowing ArgoCD to sync the rest of the monitoring stack.
**Key Lesson Learned**: Large CRDs often require `--server-side` apply to bypass client-side annotation size limits.

### 6. Grafana blank dark screen
**Symptom**: Accessing the Grafana Load Balancer URL resulted in a blank dark screen or a timeout.
**Root Cause**: Kubernetes defaulted to creating an AWS Classic Load Balancer (CLB), which failed to correctly route traffic to the EKS nodes.
**Commands to Diagnose**:
```bash
kubectl get svc -n monitoring
```
**Commands to Fix**:
Add the Network Load Balancer (NLB) annotation to the Grafana service values:
```yaml
service.beta.kubernetes.io/aws-load-balancer-type: nlb
```
**Outcome**: AWS provisioned an NLB, and traffic successfully routed to the Grafana pods.
**Key Lesson Learned**: Always explicitly specify the AWS load balancer type (NLB or ALB) when exposing services on EKS.

### 7. Grafana NLB resolving to internal IPs
**Symptom**: The browser returned `DNS_PROBE_FINISHED_NXDOMAIN` or timed out when accessing the NLB URL.
**Root Cause**: By default, AWS NLBs created by Kubernetes are internal-only. They were not resolvable/reachable from the public internet.
**Commands to Fix**:
Add the internet-facing annotation:
```yaml
service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing
```
Delete the existing service via ArgoCD or `kubectl` to force the Load Balancer Controller to recreate it from scratch:
```bash
kubectl delete svc <grafana-service> -n monitoring
```
**Outcome**: A new public-facing NLB was provisioned, resolving to public IPs.
**Key Lesson Learned**: Modifying LB schemes often requires deleting and recreating the Kubernetes Service object.

### 8. StatefulSet immutable field error
**Symptom**: Applying an update to the Postgres StatefulSet failed with an immutability error.
**Root Cause**: Kubernetes does not allow modifying certain fields on an existing StatefulSet, notably `volumeClaimTemplates`.
**Commands to Diagnose**:
Check ArgoCD sync errors or `kubectl apply` output.
**Commands to Fix**:
Delete the StatefulSet (this does not delete the underlying PersistentVolume or PersistentVolumeClaim):
```bash
kubectl delete statefulset postgres -n trading
```
Let ArgoCD automatically recreate it with the new specifications.
**Outcome**: StatefulSet was recreated successfully and reattached to the existing data volume.
**Key Lesson Learned**: Understand which Kubernetes resource fields are immutable and plan update strategies accordingly.

### 9. ArgoCD sync error: --force cannot be used with --server-side
**Symptom**: Manually triggering an ArgoCD sync via the UI/API failed with an error message about conflicting flags.
**Root Cause**: The user attempted to force a sync (`--force`) while Server-Side Apply was also enabled. These two flags are mutually exclusive in this context.
**Commands to Fix**:
Trigger the sync again, ensuring the `--force` option is disabled in the ArgoCD UI or CLI.
**Outcome**: Sync proceeded successfully.
**Key Lesson Learned**: Be mindful of ArgoCD sync options; force-applying should generally be avoided in favor of understanding why a standard apply is failing.

---

## General Debugging Commands

- **Check Pod Status**: `kubectl get pods -n <namespace>`
- **View Pod Logs**: `kubectl logs <pod-name> -n <namespace>`
- **Describe Pod Events**: `kubectl describe pod <pod-name> -n <namespace>`
- **Check Secrets**: `kubectl get secrets -n <namespace>`
- **Check External Secrets Status**: `kubectl get externalsecrets -n <namespace>`
- **Port Forwarding (Local Access)**: `kubectl port-forward svc/<service-name> <local-port>:<remote-port> -n <namespace>`
- **Execute into Pod**: `kubectl exec -it <pod-name> -n <namespace> -- /bin/sh`

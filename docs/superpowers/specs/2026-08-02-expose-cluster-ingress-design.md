# Expose the Cluster to the Internet — Design Spec

Date: 2026-08-02
Scope: Part I of the "Exposing the Cluster and Production-Grade Monitoring" assignment —
internet exposure via ingress-nginx + ALB + Route53, plus the monitoring-stack
consolidation and application wiring needed to make that exposure actually work
end-to-end. Alertmanager rules / advanced alerting (Part II proper) are out of scope.

## 1. Context

The cluster is a self-managed kubeadm cluster on plain EC2 (public subnets only, no
NAT), provisioned by Terraform (`infra/tf`) and bootstrapped over SSH
(`scripts/bootstrap.sh`, run by `.github/workflows/cluster.yaml`). Bootstrap installs
Calico, metrics-server, the AWS EBS CSI driver, and ArgoCD via raw `kubectl apply`,
then applies `infra/argocd/app-of-apps.yaml`, which recursively syncs every manifest
under `infra/argocd/`. Two of those Applications (`dev.yaml`, `prod.yaml`) each
recursively sync `infra/k8s/dev` / `infra/k8s/prod` into namespaces `dev` / `prod`;
a third (`cluster-resources.yaml`) recursively syncs `infra/k8s/common` with no fixed
namespace. Everything is currently reachable only via `kubectl port-forward`.

**Important structural constraint discovered during design:** any Application using
`directory: recurse: true` (`app-of-apps`, `dev`, `prod`, `cluster-resources`) will
`kubectl apply` *every* file under its path. A plain Helm `values.yaml` is not a valid
Kubernetes manifest, so it must never live under `infra/argocd/`, `infra/k8s/dev`,
`infra/k8s/prod`, or `infra/k8s/common` — it would break that Application's sync. Helm
values therefore live under a new `infra/helm/` path, referenced only via ArgoCD's
multi-source `$values` git ref, never recursed as manifests.

## 2. Architecture

```
Internet → Route53 (fursa.click zone, data source only — zone itself not managed)
         → ALB (internet-facing, public subnets, HTTPS:443, ACM cert)
         → Target Group (instance targets = worker ASG members, port 30080)
         → ingress-nginx NodePort Service (30080 HTTP / 30443 HTTPS, pinned via Helm values)
         → ingress-nginx controller (DaemonSet, one pod per worker node)
         → Ingress resources (host/path routing) → in-cluster Services
```

The ALB terminates TLS with an ACM certificate and forwards plain HTTP to the
ingress-nginx NodePort. ingress-nginx does all host/path routing inside the cluster.
No TLS or cert-manager is needed inside the cluster. NodePort 30443 is pinned (per
the assignment's explicit requirement to pin both ports) but not wired to anything
today — nothing currently needs HTTPS passthrough.

**ingress-nginx runs as a DaemonSet with `externalTrafficPolicy: Local`.** This
guarantees every worker ASG instance always has a local controller pod, so the ALB
target group — which registers all ASG instances via `aws_autoscaling_attachment` —
is always checking nodes that can actually serve traffic. It self-corrects as the ASG
scales up/down, with no orphaned or permanently-unhealthy targets.

## 3. Hostnames

All five hostnames use the `moataz-` prefix, are exact (no wildcards — nothing here
needs open-ended subdomains), and are the ACM certificate's SANs and the Route53
records, one-to-one:

| Hostname | Backend | Namespace |
|---|---|---|
| `moataz-dev.fursa.click` | frontend-svc (`/`), agent-svc (`/api`) | `dev` |
| `moataz-prod.fursa.click` | frontend-svc (`/`), agent-svc (`/api`) | `prod` |
| `moataz-grafana.fursa.click` | Grafana | `monitoring` |
| `moataz-prometheus.fursa.click` | Prometheus | `monitoring` |
| `moataz-argocd.fursa.click` | argocd-server | `argocd` |

Grafana/Prometheus/ArgoCD are single shared instances (one monitoring stack, one
ArgoCD control plane) — not duplicated per environment, even though Frontend/Agent
are.

## 4. Terraform

### 4.1 `modules/k8s-cluster` (small addition)

Add one output: `worker_security_group_id` (the existing `aws_security_group.workers`
id). Needed so the new ingress module can open a narrow ingress rule into it without
modifying the security group resource itself.

### 4.2 New `modules/ingress`

Inputs: `project_name`, `vpc_id`, `public_subnet_ids`, `worker_asg_name`,
`worker_security_group_id`, `worker_http_node_port` (30080), `route53_zone_name`
(`fursa.click`), `hostnames` (the 5-entry list above).

Resources:
- `aws_security_group.alb` — ingress 443 from `0.0.0.0/0`, egress all.
- `aws_security_group_rule` on the **existing** workers SG — ingress TCP 30080 from
  the ALB SG only (least privilege; 30443 isn't opened since nothing targets it).
- `aws_lb` (internet-facing, `public_subnet_ids`).
- `aws_lb_target_group` — `target_type = "instance"`, protocol HTTP, port 30080.
  Health check: protocol HTTP, port `traffic-port` (30080), path `/`, **matcher
  `404`** (exact, not a range). Justification: ALB's health check never sends a
  `Host` header matching one of our configured Ingress hosts, so it deterministically
  falls through to ingress-nginx's built-in default backend, which always returns
  exactly 404 whenever nginx itself is alive and serving on the data-plane port. This
  is verified empirically post-install (see §8 Validation), not assumed.
- `aws_lb_listener` — port 443, HTTPS, ACM cert, default action = forward to the
  target group. No listener rules needed — all host/path routing happens inside
  ingress-nginx, not at the ALB.
- `aws_autoscaling_attachment` — wires the target group to `worker_asg_name`.
- `aws_acm_certificate` (DNS validation) + `aws_route53_record` (validation CNAMEs)
  + `aws_acm_certificate_validation`. SANs = the 5 hostnames in §3.
- `data "aws_route53_zone" "fursa_click"` — lookup only, zone is never managed/created
  or destroyed by this stack.
- 5× `aws_route53_record` (type A, alias to the ALB) — one per hostname in §3.

Root `infra/tf/main.tf` wires this module using `module.k8s_cluster`'s outputs (vpc
id, public subnets, ASG name, new SG id). `infra/tf/tfvars/us-east-1.tfvars` gets a
`route53_zone_name = "fursa.click"` entry.

## 5. ingress-nginx (Helm chart via ArgoCD, not manifest+patch)

New `infra/argocd/ingress-nginx.yaml` Application, multi-source:
- Helm chart `ingress-nginx` from `https://kubernetes.github.io/ingress-nginx`,
  `targetRevision: "4.14.1"` (controller `v1.14.1` — latest available; the
  `kubernetes/ingress-nginx` project was archived in March 2026 with best-effort
  maintenance only going forward. Noted as a caveat, not a scope change — the
  assignment explicitly specifies the ingress-nginx controller/chart.)
- `$values` ref to `infra/helm/ingress-nginx-values.yaml` (git-sourced, see §1
  constraint on why this can't live under a recursed manifest path).
- Destination namespace `ingress-nginx`, `CreateNamespace=true`, automated
  prune+selfHeal.

`infra/helm/ingress-nginx-values.yaml` sets:
```yaml
controller:
  kind: DaemonSet
  service:
    type: NodePort
    externalTrafficPolicy: Local
    nodePorts:
      http: 30080
      https: 30443
```

This fully replaces the originally-considered "kubectl apply the baremetal manifest,
then kubectl patch the Service" approach in `bootstrap.sh` — `bootstrap.sh` needs no
ingress-nginx-related changes at all now; ArgoCD handles it end-to-end via
`app-of-apps`.

## 6. Kubernetes Ingress resources

### 6.1 Frontend / Agent (per env: `infra/k8s/dev/`, `infra/k8s/prod/`)

**Two separate Ingress objects sharing the same host** (nginx merges same-host rules
from multiple Ingress objects into one server block; annotations apply per-Ingress-
object, so scoping the rewrite to only the agent object is correct and was the
specific bug in the earlier draft):

- `frontend-ingress` — host `{env-host}`, path `/` → `frontend-svc:3000`. No
  annotations.
- `agent-ingress` — host `{env-host}`, path `/api(/|$)(.*)` → `agent-svc:8000`.
  Annotations: `nginx.ingress.kubernetes.io/use-regex: "true"`,
  `nginx.ingress.kubernetes.io/rewrite-target: /$2` (agent's own routes are `/chat`,
  `/health`, `/ready` at root, so the `/api` prefix must be stripped before
  forwarding).

### 6.2 Grafana / Prometheus (`infra/k8s/common/monitoring/`)

- `grafana-ingress` — host `moataz-grafana.fursa.click` → Service `grafana`
  (namespace `monitoring`), port 80.
- `prometheus-ingress` — host `moataz-prometheus.fursa.click` → Service
  `monitoring-prometheus` (namespace `monitoring`), port 9090. **Not**
  `prometheus-operated` — that's the Prometheus Operator's headless governing
  Service (used for StatefulSet pod peer discovery), not an intentionally-exposed
  regular Service. Setting the chart's top-level `fullnameOverride: monitoring` in
  Helm values makes the chart's own regular ClusterIP Prometheus Service
  deterministically named `monitoring-prometheus` — that's the correct public
  backend. This is verified with `kubectl get svc -n monitoring` after install (see
  §8), not assumed.

### 6.3 ArgoCD (`infra/k8s/common/argocd/`)

Single `argocd-ingress` — host `moataz-argocd.fursa.click`, explicit
`metadata.namespace: argocd`, → Service `argocd-server:80`. Lives under
`infra/k8s/common` (synced once, by `cluster-resources.yaml`) rather than duplicated
under `dev`/`prod`, since there is exactly one ArgoCD control plane, not one per
environment — an Ingress object cannot target a Service in a different namespace, so
duplicating it into `dev`/`prod` would require the same `namespace: argocd` override
twice for no benefit.

`scripts/bootstrap.sh` gets one addition: after installing ArgoCD, patch the
`argocd-cmd-params-cm` ConfigMap to set `server.insecure: "true"` and roll the
`argocd-server` Deployment. Required because ingress-nginx forwards plain HTTP, and
stock ArgoCD only serves gRPC/HTTPS — without this the ArgoCD ingress cannot work.

## 7. Shared kube-prometheus-stack (replaces per-env raw manifests)

Confirmed via inspection: the repo currently has **duplicated raw manifests** per
environment (`infra/k8s/{dev,prod}/grafana/*`, `infra/k8s/{dev,prod}/prometheus/*`),
not an existing shared/intentional-separate Helm-based stack. Per direction, these
are deleted and replaced by one shared release.

New `infra/argocd/monitoring.yaml` Application, multi-source:
- Helm chart `kube-prometheus-stack` from
  `https://prometheus-community.github.io/helm-charts`, `targetRevision: "88.0.1"`.
- `$values` ref to `infra/helm/monitoring-values.yaml`.
- Destination namespace `monitoring`, `CreateNamespace=true`, automated
  prune+selfHeal.

`infra/helm/monitoring-values.yaml` sets:
```yaml
fullnameOverride: monitoring
prometheus:
  prometheusSpec:
    serviceMonitorSelector: {}
    serviceMonitorNamespaceSelector: {}
    serviceMonitorSelectorNilUsesHelmValues: false
grafana:
  fullnameOverride: grafana
  sidecar:
    dashboards:
      enabled: true
      label: grafana_dashboard
      labelValue: "1"
```
- `serviceMonitorSelector: {}` + `serviceMonitorNamespaceSelector: {}` +
  `serviceMonitorSelectorNilUsesHelmValues: false`: watch ServiceMonitors in *any*
  namespace (including `dev`/`prod`), matching any labels — explicit, not relying on
  chart defaults.
- `grafana.sidecar.dashboards`: explicitly enabled (not just relying on the chart
  default) with a `label`/`labelValue` that the migrated dashboard ConfigMap (§7.2)
  must match exactly.

### 7.1 ServiceMonitors (co-located with each service, per env)

New files: `infra/k8s/{dev,prod}/yolo/yolo-servicemonitor.yaml`,
`infra/k8s/{dev,prod}/agent/agent-servicemonitor.yaml`. Each: `namespaceSelector.
matchNames: [dev]` (or `[prod]`), `selector.matchLabels: {app: yolo}` (or `{app:
agent}`), one endpoint referencing the Service's **named** port `http`, path
`/metrics`, `interval: 15s` (matching the scrape interval from the raw
`prometheus-configmap.yaml` being replaced).

Requires editing the existing Service manifests, which currently have neither a
label nor a named port:
- `infra/k8s/{dev,prod}/yolo/yolo-service.yaml`: add `metadata.labels: {app: yolo}`,
  rename the port to `name: http`.
- `infra/k8s/{dev,prod}/agent/agent-service.yaml`: add `metadata.labels: {app:
  agent}`, name the port `http`.

(`yolo` already exposes `/metrics` via `prometheus_fastapi_instrumentator`, confirmed
in `services/yolo/app.py`. `agent` does not currently expose `/metrics` — its
ServiceMonitor is added anyway for parity with the raw config it replaces, which
already scraped it; that target will simply show as down in Prometheus, which is a
pre-existing gap, not a regression introduced here, and instrumenting the agent app
is out of scope.)

### 7.2 Grafana dashboard migration

`infra/grafana/dashboards/agent.json` becomes a ConfigMap at
`infra/k8s/common/monitoring/grafana-agent-dashboard.yaml`, `metadata.labels:
{grafana_dashboard: "1"}` (must exactly match `grafana.sidecar.dashboards.label`/
`labelValue` above), namespace `monitoring`. A `namespace` template variable is added
to the dashboard, and panel queries are updated to filter by it (e.g.
`agent_chat_requests_total{namespace=~"$namespace"}`). This works with zero extra
relabeling because the Prometheus Operator automatically attaches a `namespace`
label (the scraped target's own namespace) to every series collected via a
ServiceMonitor.

Alertmanager ships with the chart by default and is untouched — not exposed via
ingress (wasn't requested; only Grafana and Prometheus were named for exposure).

## 8. Frontend ↔ Agent wiring (`cd.yml`, `Dockerfile`)

`services/frontend/lib/api.ts` reads `NEXT_PUBLIC_AGENT_URL` (a Next.js **build-time**
env var) and falls back to `http://localhost:8000` — currently never set at build
time, so the browser-side chat call is broken as deployed today, independent of this
task. The `Dockerfile` already has `ARG NEXT_PUBLIC_AGENT_URL` / `ENV
NEXT_PUBLIC_AGENT_URL=$NEXT_PUBLIC_AGENT_URL`; `cd.yml` never passes it.

Fix: the `frontend` entry in `cd.yml`'s build matrix gets `build-args:
NEXT_PUBLIC_AGENT_URL=/api`, passed through to `docker/build-push-action`'s
`build-args` input. A relative `/api` works identically on both
`moataz-dev.fursa.click` and `moataz-prod.fursa.click` since it resolves against
whatever origin served the page — no per-environment image needed.

## 9. File-level summary

**New:**
- `infra/tf/modules/ingress/{main,variables,outputs}.tf`
- `infra/helm/ingress-nginx-values.yaml`
- `infra/helm/monitoring-values.yaml`
- `infra/argocd/ingress-nginx.yaml`
- `infra/argocd/monitoring.yaml`
- `infra/k8s/common/argocd/argocd-ingress.yaml`
- `infra/k8s/common/monitoring/grafana-ingress.yaml`
- `infra/k8s/common/monitoring/prometheus-ingress.yaml`
- `infra/k8s/common/monitoring/grafana-agent-dashboard.yaml`
- `infra/k8s/{dev,prod}/frontend/frontend-ingress.yaml`
- `infra/k8s/{dev,prod}/agent/agent-ingress.yaml`
- `infra/k8s/{dev,prod}/yolo/yolo-servicemonitor.yaml`
- `infra/k8s/{dev,prod}/agent/agent-servicemonitor.yaml`

**Modified:**
- `infra/tf/main.tf`, `infra/tf/variables.tf`, `infra/tf/outputs.tf`,
  `infra/tf/tfvars/us-east-1.tfvars`
- `infra/tf/modules/k8s-cluster/outputs.tf` (new `worker_security_group_id` output)
- `scripts/bootstrap.sh` (ArgoCD insecure-mode patch only)
- `.github/workflows/cd.yml` (frontend `build-args`)
- `infra/k8s/{dev,prod}/yolo/yolo-service.yaml`,
  `infra/k8s/{dev,prod}/agent/agent-service.yaml` (label + named port)

**Deleted:**
- `infra/k8s/dev/grafana/`, `infra/k8s/dev/prometheus/`
- `infra/k8s/prod/grafana/`, `infra/k8s/prod/prometheus/`
- `infra/grafana/dashboards/agent.json` (migrated into the ConfigMap above)

## 10. Validation

- `kubectl get pods -n ingress-nginx` — one controller pod per worker node (DaemonSet).
- `kubectl get svc -n ingress-nginx ingress-nginx-controller` — NodePorts are exactly
  30080/30443.
- **ALB health-check assumption check (required, not optional):** from outside the
  cluster, `curl -s -o /dev/null -w "%{http_code}" -H "Host: unmatched.invalid"
  http://<worker-public-ip>:30080/` against a real worker node and confirm it returns
  exactly `404`, consistently, before trusting the target group's health state.
- AWS Console/CLI: ALB provisioned, listener 443 present, target group shows all ASG
  instances `healthy`.
- `dig`/`curl` each of the 5 `moataz-*.fursa.click` hostnames resolves to the ALB and
  serves the expected backend over HTTPS.
- `kubectl get svc -n monitoring` — confirm the Prometheus Service is named
  `monitoring-prometheus` (not `prometheus-operated`) before wiring/trusting the
  Ingress backend name.
- Grafana dashboard sidecar picks up the migrated Agent dashboard (visible in the
  Grafana UI, `Agent` folder) and the `namespace` variable filters correctly.
- `kubectl get servicemonitors -A` and Prometheus's own Targets page show `yolo`
  targets up in both `dev` and `prod`.
- No `kubectl port-forward` needed for Frontend, Agent, Grafana, Prometheus, or
  ArgoCD.
- `terraform destroy` in `infra/tf` followed by re-running the `cluster.yaml`
  provisioning workflow and normal ArgoCD bootstrap recreates everything above with
  no manual steps.

## 11. Git workflow

1. New feature branch off latest `main`.
2. Implement via the Superpowers TDD/plan-driven workflow.
3. Merge feature branch into `dev` (regular merge, no PR).
4. Return to the feature branch, push it, open a PR feature→`main`.

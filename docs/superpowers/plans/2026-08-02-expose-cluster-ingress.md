# Expose the Cluster to the Internet — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the PolyAI stack to the internet through an ALB → ingress-nginx path, replace the duplicated per-env Grafana/Prometheus manifests with one shared `kube-prometheus-stack` release, and wire the frontend's chat call through the new `/api` ingress path — all reproducible from code (Terraform + ArgoCD), with no `kubectl port-forward`.

**Architecture:** Route53 (data-sourced `fursa.click` zone) → ALB (HTTPS:443, ACM cert) → Target Group (worker ASG instances, NodePort 30080) → ingress-nginx (DaemonSet, `externalTrafficPolicy: Local`, fixed NodePorts 30080/30443, installed via a version-pinned ArgoCD Helm Application) → per-host/path Ingress resources → in-cluster Services. Monitoring consolidates onto one `kube-prometheus-stack` Helm release in namespace `monitoring`, scraping `dev` and `prod` via ServiceMonitors, also installed via an ArgoCD Helm Application.

**Tech Stack:** Terraform (AWS provider `~> 6.0`), ArgoCD (multi-source Helm Applications), Helm charts `ingress-nginx` `4.14.1` and `kube-prometheus-stack` `88.0.1`, Kubernetes networking.k8s.io/v1 Ingress + monitoring.coreos.com/v1 ServiceMonitor, GitHub Actions.

## Global Constraints

- All 5 public hostnames use the `moataz-` prefix, are exact (no wildcards): `moataz-dev.fursa.click`, `moataz-prod.fursa.click`, `moataz-grafana.fursa.click`, `moataz-prometheus.fursa.click`, `moataz-argocd.fursa.click`.
- The `fursa.click` Route53 hosted zone is looked up via `data "aws_route53_zone"` only — never created, managed, or destroyable by this stack.
- ingress-nginx NodePorts are pinned: HTTP `30080`, HTTPS `30443`.
- ALB target group health check: path `/`, port `traffic-port` (30080), matcher exactly `404` (not a range) — verified empirically post-deploy, not assumed.
- Prometheus's public-facing Service must be the chart-rendered regular Service (`monitoring-prometheus`, via `fullnameOverride: monitoring`), never the Operator's headless `prometheus-operated` governing Service.
- `prometheus.prometheusSpec.serviceMonitorSelector: {}`, `serviceMonitorNamespaceSelector: {}`, `serviceMonitorSelectorNilUsesHelmValues: false` — ServiceMonitors in any namespace, any labels, are picked up.
- Any file that is Helm *values* (not a K8s manifest) must live under `infra/helm/`, never under `infra/argocd/`, `infra/k8s/dev`, `infra/k8s/prod`, or `infra/k8s/common` — those four paths are `directory: recurse: true` targets and `kubectl apply` everything under them.
- Frontend/Agent ingress: two separate Ingress objects per env sharing one host (never one Ingress with rewrite annotations covering both paths).
- `NEXT_PUBLIC_AGENT_URL` must be `/api` (relative), wired via Docker build-arg in `cd.yml`, not hardcoded into application source.
- Never run `terraform apply` or trigger the `cluster.yaml` GitHub Actions workflow during this implementation — this plan produces code and read-only validation (`terraform plan`, `helm template`, YAML syntax checks) only. Actual provisioning happens later, out of band, with separate explicit authorization.
- Git workflow at the end: commit on `feature/expose-cluster-ingress` → regular merge into `dev` (no PR) → return to `feature/expose-cluster-ingress` → push → open PR to `main`. No new branches.

---

## File Structure

**New files:**
- `infra/tf/modules/ingress/{variables,main,outputs}.tf` — ALB, ACM, Route53, SG rule, ASG attachment
- `infra/helm/ingress-nginx-values.yaml`, `infra/helm/monitoring-values.yaml` — Helm values, referenced only via ArgoCD `$values` refs
- `infra/argocd/ingress-nginx.yaml`, `infra/argocd/monitoring.yaml` — ArgoCD multi-source Helm Applications
- `infra/k8s/common/argocd/argocd-ingress.yaml` — shared ArgoCD ingress
- `infra/k8s/common/monitoring/grafana-ingress.yaml`, `.../prometheus-ingress.yaml`, `.../grafana-agent-dashboard.yaml` — shared monitoring ingress + migrated dashboard
- `infra/k8s/{dev,prod}/frontend/frontend-ingress.yaml`, `infra/k8s/{dev,prod}/agent/agent-ingress.yaml` — per-env path-split ingress
- `infra/k8s/{dev,prod}/yolo/yolo-servicemonitor.yaml`, `infra/k8s/{dev,prod}/agent/agent-servicemonitor.yaml`

**Modified files:**
- `infra/tf/modules/k8s-cluster/outputs.tf` — new `worker_security_group_id` output
- `infra/tf/main.tf`, `infra/tf/variables.tf`, `infra/tf/outputs.tf`, `infra/tf/tfvars/us-east-1.tfvars`
- `scripts/bootstrap.sh` — ArgoCD insecure-mode patch
- `.github/workflows/cd.yml` — frontend `build-args`
- `infra/k8s/{dev,prod}/yolo/yolo-service.yaml`, `infra/k8s/{dev,prod}/agent/agent-service.yaml` — add `app` label + name the port `http`

**Deleted files:**
- `infra/k8s/dev/grafana/`, `infra/k8s/dev/prometheus/`, `infra/k8s/prod/grafana/`, `infra/k8s/prod/prometheus/` (entire directories)
- `infra/grafana/dashboards/agent.json` (migrated, not discarded)

---

### Task 1: `modules/k8s-cluster` — expose the workers security group ID

**Files:**
- Modify: `infra/tf/modules/k8s-cluster/outputs.tf`

**Interfaces:**
- Produces: output `worker_security_group_id` (string) — consumed by `modules/ingress` in Task 3.

- [ ] **Step 1: Add the output**

Append to `infra/tf/modules/k8s-cluster/outputs.tf`:

```hcl
output "worker_security_group_id" {
  description = "ID of the worker nodes security group"
  value       = aws_security_group.workers.id
}
```

- [ ] **Step 2: Validate**

```bash
cd infra/tf && terraform fmt -check modules/k8s-cluster/outputs.tf
```
Expected: no output (already formatted). If it prints the filename, run `terraform fmt modules/k8s-cluster/outputs.tf` and re-check.

- [ ] **Step 3: Commit**

```bash
git add infra/tf/modules/k8s-cluster/outputs.tf
git commit -m "terraform: expose worker security group id from k8s-cluster module"
```

---

### Task 2: `modules/ingress` — variables

**Files:**
- Create: `infra/tf/modules/ingress/variables.tf`

**Interfaces:**
- Consumes: nothing (leaf module input file)
- Produces: variable names consumed by `main.tf` in Task 3: `project_name`, `vpc_id`, `public_subnet_ids`, `worker_asg_name`, `worker_security_group_id`, `worker_http_node_port` (default `30080`), `route53_zone_name`, `hostnames` (`list(string)`, first element is the ACM cert's primary domain).

- [ ] **Step 1: Write the file**

```hcl
variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "vpc_id" {
  description = "ID of the VPC"
  type        = string
}

variable "public_subnet_ids" {
  description = "IDs of the public subnets the ALB will be deployed into"
  type        = list(string)
}

variable "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group to attach to the target group"
  type        = string
}

variable "worker_security_group_id" {
  description = "ID of the worker nodes security group"
  type        = string
}

variable "worker_http_node_port" {
  description = "Fixed NodePort ingress-nginx listens on for HTTP"
  type        = number
  default     = 30080
}

variable "route53_zone_name" {
  description = "Name of the existing Route53 hosted zone (looked up via data source, never managed)"
  type        = string
}

variable "hostnames" {
  description = "Public hostnames to create ACM SANs and Route53 alias records for; the first entry is the ACM certificate's primary domain, the rest become subject_alternative_names"
  type        = list(string)

  validation {
    condition     = length(var.hostnames) > 0
    error_message = "hostnames must contain at least one entry."
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add infra/tf/modules/ingress/variables.tf
git commit -m "terraform: add ingress module variables"
```

---

### Task 3: `modules/ingress` — main resources (SG, ACM, ALB, target group, listener, ASG attachment, Route53)

**Files:**
- Create: `infra/tf/modules/ingress/main.tf`

**Interfaces:**
- Consumes: all variables from Task 2; `aws_security_group.workers`'s id via `var.worker_security_group_id` (Task 1's output, passed in by the root module in Task 5).
- Produces: resources `aws_lb.this`, `aws_lb_target_group.http_node_port`, `aws_acm_certificate_validation.this` — consumed by `outputs.tf` in Task 4.

- [ ] **Step 1: Write the file**

```hcl
data "aws_route53_zone" "this" {
  name         = var.route53_zone_name
  private_zone = false
}

# =========================================================
# ALB Security Group
# =========================================================

resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb-sg"
  description = "Security group for the internet-facing ALB"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Allow all outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-alb-sg"
  }
}

# Allow the ALB to reach ingress-nginx's fixed HTTP NodePort on every worker.
resource "aws_security_group_rule" "workers_from_alb_http_node_port" {
  type                     = "ingress"
  description              = "ingress-nginx HTTP NodePort from the ALB"
  from_port                = var.worker_http_node_port
  to_port                  = var.worker_http_node_port
  protocol                 = "tcp"
  security_group_id        = var.worker_security_group_id
  source_security_group_id = aws_security_group.alb.id
}

# =========================================================
# ACM Certificate (DNS validated in the looked-up zone)
# =========================================================

resource "aws_acm_certificate" "this" {
  domain_name               = var.hostnames[0]
  subject_alternative_names = slice(var.hostnames, 1, length(var.hostnames))
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${var.project_name}-alb-cert"
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.this.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  }

  zone_id         = data.aws_route53_zone.this.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# =========================================================
# Application Load Balancer
# =========================================================

resource "aws_lb" "this" {
  name               = "${var.project_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  tags = {
    Name = "${var.project_name}-alb"
  }
}

resource "aws_lb_target_group" "http_node_port" {
  name        = "${var.project_name}-ingress-tg"
  port        = var.worker_http_node_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  # Health check hits the ingress-nginx data-plane port directly (not the
  # operator-only 10254 metrics port). ALB health checks never send a Host
  # header matching one of our configured Ingress hosts, so the request
  # deterministically falls through to nginx's built-in default backend,
  # which always returns exactly 404 whenever nginx itself is alive and
  # serving. Matcher is exact (404), not a guessed range — verified against
  # a real worker NodePort post-deploy (see plan Task 22).
  health_check {
    protocol            = "HTTP"
    port                = "traffic-port"
    path                = "/"
    matcher             = "404"
    interval            = 30
    timeout              = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = {
    Name = "${var.project_name}-ingress-tg"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.this.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.http_node_port.arn
  }
}

resource "aws_autoscaling_attachment" "workers" {
  autoscaling_group_name = var.worker_asg_name
  lb_target_group_arn    = aws_lb_target_group.http_node_port.arn
}

# =========================================================
# Route53 alias records -> ALB (one per public hostname)
# =========================================================

resource "aws_route53_record" "alb_alias" {
  for_each = toset(var.hostnames)

  zone_id = data.aws_route53_zone.this.zone_id
  name    = each.value
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}
```

- [ ] **Step 2: Fix formatting**

```bash
cd infra/tf && terraform fmt modules/ingress/main.tf
```
Expected: reformats the `health_check` block's aligned `=` signs (the block above is deliberately hand-aligned and may need `fmt` to normalize spacing — that's fine, `fmt` output is authoritative).

- [ ] **Step 3: Commit**

```bash
git add infra/tf/modules/ingress/main.tf
git commit -m "terraform: add ingress module (ALB, ACM, Route53, SG rule, ASG attachment)"
```

---

### Task 4: `modules/ingress` — outputs

**Files:**
- Create: `infra/tf/modules/ingress/outputs.tf`

**Interfaces:**
- Consumes: resources from Task 3.
- Produces: `alb_dns_name`, `alb_zone_id`, `target_group_arn`, `certificate_arn`, `hostnames` — consumed by root `outputs.tf` in Task 5.

- [ ] **Step 1: Write the file**

```hcl
output "alb_dns_name" {
  description = "DNS name of the ingress ALB"
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "Route53 hosted zone ID of the ALB (for alias records)"
  value       = aws_lb.this.zone_id
}

output "target_group_arn" {
  description = "ARN of the ALB target group forwarding to the ingress-nginx NodePort"
  value       = aws_lb_target_group.http_node_port.arn
}

output "certificate_arn" {
  description = "ARN of the validated ACM certificate used by the HTTPS listener"
  value       = aws_acm_certificate_validation.this.certificate_arn
}

output "hostnames" {
  description = "Public hostnames routed through the ALB"
  value       = var.hostnames
}
```

- [ ] **Step 2: Commit**

```bash
git add infra/tf/modules/ingress/outputs.tf
git commit -m "terraform: add ingress module outputs"
```

---

### Task 5: Wire the ingress module into the root Terraform stack

**Files:**
- Modify: `infra/tf/main.tf`
- Modify: `infra/tf/variables.tf`
- Modify: `infra/tf/outputs.tf`
- Modify: `infra/tf/tfvars/us-east-1.tfvars`

**Interfaces:**
- Consumes: `module.k8s_cluster.worker_asg_name` (existing), `module.k8s_cluster.worker_security_group_id` (Task 1), `module.vpc.vpc_id` / `module.vpc.public_subnets` (existing).

- [ ] **Step 1: Add `route53_zone_name` variable**

Append to `infra/tf/variables.tf`:

```hcl
variable "route53_zone_name" {
  description = "Name of the existing Route53 hosted zone used for public DNS records"
  type        = string
}
```

- [ ] **Step 2: Add the module block**

Append to `infra/tf/main.tf` (after the existing `module "k8s_cluster"` block):

```hcl
locals {
  ingress_hostnames = [
    "moataz-dev.fursa.click",
    "moataz-prod.fursa.click",
    "moataz-grafana.fursa.click",
    "moataz-prometheus.fursa.click",
    "moataz-argocd.fursa.click",
  ]
}

module "ingress" {
  source = "./modules/ingress"

  project_name              = var.project_name
  vpc_id                    = module.vpc.vpc_id
  public_subnet_ids         = module.vpc.public_subnets
  worker_asg_name           = module.k8s_cluster.worker_asg_name
  worker_security_group_id  = module.k8s_cluster.worker_security_group_id
  route53_zone_name         = var.route53_zone_name
  hostnames                 = local.ingress_hostnames
}
```

- [ ] **Step 3: Add root outputs**

Append to `infra/tf/outputs.tf`:

```hcl
# ==========================================
# Ingress / ALB
# ==========================================

output "alb_dns_name" {
  description = "DNS name of the ingress ALB"
  value       = module.ingress.alb_dns_name
}

output "ingress_hostnames" {
  description = "Public hostnames routed through the ALB"
  value       = module.ingress.hostnames
}
```

- [ ] **Step 4: Add the tfvars value**

Append to `infra/tf/tfvars/us-east-1.tfvars`:

```hcl
route53_zone_name = "fursa.click"
```

- [ ] **Step 5: Format and validate (no backend access, no state touched)**

```bash
cd infra/tf
terraform fmt -recursive
terraform init -backend=false -input=false
terraform validate
```
Expected: `terraform validate` prints `Success! The configuration is valid.` `terraform fmt -recursive` should produce no further diffs on a second run.

- [ ] **Step 6: Commit**

```bash
git add infra/tf/main.tf infra/tf/variables.tf infra/tf/outputs.tf infra/tf/tfvars/us-east-1.tfvars
git commit -m "terraform: wire ingress module into root stack"
```

---

### Task 6: `terraform plan` sanity check (read-only, no apply)

**Files:** none (validation only)

- [ ] **Step 1: Re-init with the real backend and plan**

```bash
cd infra/tf
terraform init -input=false
terraform workspace select -or-create us-east-1
terraform plan -var-file="tfvars/us-east-1.tfvars" -out=/tmp/expose-cluster.tfplan
```
Expected: plan succeeds and shows only **additions** — the new `module.ingress.*` resources (security group, security group rule, ACM certificate + validation, ALB, target group, listener, autoscaling attachment, 6 Route53 records) plus the one new `worker_security_group_id` output on the existing `k8s_cluster` module. **No existing resource should show as changed or destroyed.** If anything shows `~` (update in place) or `-` (destroy) on an existing resource, stop and investigate before proceeding — that would mean an unintended change to already-running infrastructure.

- [ ] **Step 2: Discard the plan file (this task never applies)**

```bash
rm -f /tmp/expose-cluster.tfplan
```

No commit for this task — it's a read-only check against real state.

---

### Task 7: ingress-nginx Helm values + ArgoCD Application

**Files:**
- Create: `infra/helm/ingress-nginx-values.yaml`
- Create: `infra/argocd/ingress-nginx.yaml`

**Interfaces:**
- Produces: NodePort Service `ingress-nginx-controller` in namespace `ingress-nginx` with fixed ports 30080/30443, referenced by the ALB target group (Task 3) and by every Ingress resource created in later tasks (`ingressClassName: nginx`).

- [ ] **Step 1: Write the Helm values**

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

`DaemonSet` + `externalTrafficPolicy: Local` guarantees a controller pod on every worker node, matching exactly what the ALB target group registers (all worker ASG instances) — so every registered target is genuinely reachable and health checks are meaningful, and it preserves the real client source IP (no extra SNAT hop).

- [ ] **Step 2: Write the ArgoCD Application**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ingress-nginx
  namespace: argocd
spec:
  project: default

  sources:
    - repoURL: https://kubernetes.github.io/ingress-nginx
      chart: ingress-nginx
      targetRevision: "4.14.1"
      helm:
        valueFiles:
          - $values/infra/helm/ingress-nginx-values.yaml
    - repoURL: https://github.com/moataz189/polyaifursa.git
      targetRevision: main
      ref: values

  destination:
    server: https://kubernetes.default.svc
    namespace: ingress-nginx

  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

Chart `4.14.1` is controller `v1.14.1`, the latest available release (the upstream `kubernetes/ingress-nginx` project was archived in March 2026 — best-effort maintenance only going forward, no further releases expected. This doesn't change the plan: the assignment explicitly specifies the ingress-nginx controller/chart.)

- [ ] **Step 3: Commit**

```bash
git add infra/helm/ingress-nginx-values.yaml infra/argocd/ingress-nginx.yaml
git commit -m "argocd: install ingress-nginx via pinned Helm chart with fixed NodePorts"
```

---

### Task 8: kube-prometheus-stack Helm values + ArgoCD Application

**Files:**
- Create: `infra/helm/monitoring-values.yaml`
- Create: `infra/argocd/monitoring.yaml`

**Interfaces:**
- Produces: Services `grafana` (port 80) and `monitoring-prometheus` (port 9090) in namespace `monitoring`, consumed by the Ingress resources in Task 17. Also produces the Prometheus Operator's ServiceMonitor CRD watch scope (`serviceMonitorSelector: {}` / `serviceMonitorNamespaceSelector: {}`), consumed by the ServiceMonitors in Tasks 13–16.

- [ ] **Step 1: Write the Helm values**

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

Notes on why each value is set:
- `fullnameOverride: monitoring` makes the chart's own regular (non-headless) Prometheus Service deterministically named `monitoring-prometheus` — this is the chart-rendered access-point Service, distinct from the Prometheus Operator's headless governing Service `prometheus-operated` (used only for StatefulSet pod peer discovery, never intended as a public ingress backend).
- `serviceMonitorSelector: {}` + `serviceMonitorNamespaceSelector: {}` + `serviceMonitorSelectorNilUsesHelmValues: false`: watch ServiceMonitors in *any* namespace (including `dev`/`prod`) regardless of labels — explicit, not relying on chart defaults.
- `grafana.fullnameOverride: grafana`: deterministic Grafana Service name.
- `grafana.sidecar.dashboards`: explicitly enabled (not relying on the chart default) with a `label`/`labelValue` that the migrated dashboard ConfigMap (Task 18) is written to match exactly.

- [ ] **Step 2: Write the ArgoCD Application**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: monitoring
  namespace: argocd
spec:
  project: default

  sources:
    - repoURL: https://prometheus-community.github.io/helm-charts
      chart: kube-prometheus-stack
      targetRevision: "88.0.1"
      helm:
        valueFiles:
          - $values/infra/helm/monitoring-values.yaml
    - repoURL: https://github.com/moataz189/polyaifursa.git
      targetRevision: main
      ref: values

  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring

  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

- [ ] **Step 3: Commit**

```bash
git add infra/helm/monitoring-values.yaml infra/argocd/monitoring.yaml
git commit -m "argocd: install shared kube-prometheus-stack in the monitoring namespace"
```

---

### Task 9: Render both charts locally to verify the values files and confirm Service names

**Files:** none (validation only, uses a local Helm install — a reversible dev-tool addition)

- [ ] **Step 1: Install Helm locally if not present**

```bash
which helm || brew install helm
helm version
```

- [ ] **Step 2: Add the two chart repos**

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
```

- [ ] **Step 3: Render ingress-nginx with our values**

```bash
helm template ingress-nginx ingress-nginx/ingress-nginx \
  --version 4.14.1 \
  -f infra/helm/ingress-nginx-values.yaml \
  --namespace ingress-nginx | tee /tmp/ingress-nginx-rendered.yaml | grep -A3 "kind: DaemonSet"
```
Expected: at least one `kind: DaemonSet` block for the controller (confirms `controller.kind: DaemonSet` took effect — a plain Helm default would render `kind: Deployment` instead).

```bash
grep -A20 "kind: Service" /tmp/ingress-nginx-rendered.yaml | grep -E "nodePort|externalTrafficPolicy"
```
Expected: `nodePort: 30080`, `nodePort: 30443`, and `externalTrafficPolicy: Local` all present.

- [ ] **Step 4: Render kube-prometheus-stack with our values and confirm exact Service names**

```bash
helm template monitoring prometheus-community/kube-prometheus-stack \
  --version 88.0.1 \
  -f infra/helm/monitoring-values.yaml \
  --namespace monitoring | tee /tmp/monitoring-rendered.yaml | grep -B2 "^kind: Service$"
```

Then confirm the two exact names this plan's Ingress resources (Task 17) depend on:

```bash
grep -B8 "^kind: Service$" /tmp/monitoring-rendered.yaml | grep "name: monitoring-prometheus$"
grep -B8 "^kind: Service$" /tmp/monitoring-rendered.yaml | grep "name: grafana$"
```
Expected: both greps print exactly one match each. **If either name differs from what's rendered, stop and update Task 17's Ingress `backend.service.name` to match the actual rendered name before proceeding** — this is the concrete verification the design corrections required, not an assumption.

- [ ] **Step 5: Clean up rendered files (scratch output only, not committed)**

```bash
rm -f /tmp/ingress-nginx-rendered.yaml /tmp/monitoring-rendered.yaml
```

No commit for this task — it's a read-only local rendering check.

---

### Task 10: ArgoCD insecure mode in `bootstrap.sh`

**Files:**
- Modify: `scripts/bootstrap.sh`

- [ ] **Step 1: Read the current ArgoCD section**

The file currently has (around the ArgoCD rollout-status waits):

```bash
kubectl rollout status \
deployment/argocd-applicationset-controller \
--namespace argocd \
--timeout=10m

#################################################
# App of Apps
#################################################
```

- [ ] **Step 2: Insert the insecure-mode patch between those two blocks**

```bash
kubectl rollout status \
deployment/argocd-applicationset-controller \
--namespace argocd \
--timeout=10m

#################################################
# ArgoCD insecure mode (required for plain-HTTP ingress)
#################################################

echo "Configuring ArgoCD server for insecure (HTTP) mode behind ingress-nginx..."

kubectl patch configmap argocd-cmd-params-cm \
  --namespace argocd \
  --type merge \
  -p '{"data":{"server.insecure":"true"}}'

kubectl rollout restart deployment/argocd-server --namespace argocd

kubectl rollout status \
deployment/argocd-server \
--namespace argocd \
--timeout=10m

#################################################
# App of Apps
#################################################
```

This is required because ingress-nginx forwards plain HTTP to `argocd-server:80`, and a stock ArgoCD install only serves gRPC/HTTPS — without this patch the ArgoCD ingress (Task 19) cannot work.

- [ ] **Step 3: Validate shell syntax**

```bash
bash -n scripts/bootstrap.sh
```
Expected: no output (syntax OK).

- [ ] **Step 4: Commit**

```bash
git add scripts/bootstrap.sh
git commit -m "bootstrap: run ArgoCD server in insecure mode for plain-HTTP ingress"
```

---

### Task 11: Dev frontend + agent Ingress (path split, same host)

**Files:**
- Create: `infra/k8s/dev/frontend/frontend-ingress.yaml`
- Create: `infra/k8s/dev/agent/agent-ingress.yaml`

**Interfaces:**
- Consumes: `frontend-svc:3000`, `agent-svc:8000` (existing, namespace `dev`); `ingressClassName: nginx` (Task 7).

- [ ] **Step 1: Write the frontend Ingress (no rewrite annotations)**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: frontend-ingress
  namespace: dev
spec:
  ingressClassName: nginx
  rules:
    - host: moataz-dev.fursa.click
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: frontend-svc
                port:
                  number: 3000
```

- [ ] **Step 2: Write the agent Ingress (separate object, same host, rewrite scoped only here)**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: agent-ingress
  namespace: dev
  annotations:
    nginx.ingress.kubernetes.io/use-regex: "true"
    nginx.ingress.kubernetes.io/rewrite-target: /$2
spec:
  ingressClassName: nginx
  rules:
    - host: moataz-dev.fursa.click
      http:
        paths:
          - path: /api(/|$)(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: agent-svc
                port:
                  number: 8000
```

These are two separate Ingress objects on purpose: `rewrite-target` is an annotation on the whole Ingress object, so putting both paths in one object would strip `/api` from frontend requests too. nginx merges same-host rules from multiple Ingress objects into one server block, so this works correctly as two objects.

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/dev/frontend/frontend-ingress.yaml')))" && echo OK
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/dev/agent/agent-ingress.yaml')))" && echo OK
```
Expected: `OK` printed twice.

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/dev/frontend/frontend-ingress.yaml infra/k8s/dev/agent/agent-ingress.yaml
git commit -m "k8s(dev): add path-split frontend and agent ingress on moataz-dev.fursa.click"
```

---

### Task 12: Prod frontend + agent Ingress (mirrors Task 11)

**Files:**
- Create: `infra/k8s/prod/frontend/frontend-ingress.yaml`
- Create: `infra/k8s/prod/agent/agent-ingress.yaml`

- [ ] **Step 1: Write the frontend Ingress**

Identical to Task 11's frontend Ingress except `namespace: prod` and `host: moataz-prod.fursa.click`:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: frontend-ingress
  namespace: prod
spec:
  ingressClassName: nginx
  rules:
    - host: moataz-prod.fursa.click
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: frontend-svc
                port:
                  number: 3000
```

- [ ] **Step 2: Write the agent Ingress**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: agent-ingress
  namespace: prod
  annotations:
    nginx.ingress.kubernetes.io/use-regex: "true"
    nginx.ingress.kubernetes.io/rewrite-target: /$2
spec:
  ingressClassName: nginx
  rules:
    - host: moataz-prod.fursa.click
      http:
        paths:
          - path: /api(/|$)(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: agent-svc
                port:
                  number: 8000
```

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/prod/frontend/frontend-ingress.yaml')))" && echo OK
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/prod/agent/agent-ingress.yaml')))" && echo OK
```

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/prod/frontend/frontend-ingress.yaml infra/k8s/prod/agent/agent-ingress.yaml
git commit -m "k8s(prod): add path-split frontend and agent ingress on moataz-prod.fursa.click"
```

---

### Task 13: Dev `yolo-svc` — add label + named port, add ServiceMonitor

**Files:**
- Modify: `infra/k8s/dev/yolo/yolo-service.yaml`
- Create: `infra/k8s/dev/yolo/yolo-servicemonitor.yaml`

**Interfaces:**
- Produces: Service `yolo-svc` with `metadata.labels.app: yolo` and a named port `http`, consumed by the ServiceMonitor's `selector`/`endpoints[].port`.

- [ ] **Step 1: Edit the Service (add label, name the port)**

Replace the full content of `infra/k8s/dev/yolo/yolo-service.yaml` with:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: yolo-svc
  namespace: dev
  labels:
    app: yolo
spec:
  selector:
    app: yolo
  ports:
    - name: http
      port: 8080
      targetPort: 8080
```

- [ ] **Step 2: Write the ServiceMonitor**

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: yolo
  namespace: dev
spec:
  namespaceSelector:
    matchNames:
      - dev
  selector:
    matchLabels:
      app: yolo
  endpoints:
    - port: http
      path: /metrics
      interval: 15s
```

`yolo` already exposes `/metrics` via `prometheus_fastapi_instrumentator` (confirmed in `services/yolo/app.py`). The Prometheus Operator automatically attaches a `namespace: dev` label to every series scraped through this ServiceMonitor — no extra relabeling needed for the dashboard's namespace filter (Task 18) to work.

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/dev/yolo/yolo-service.yaml')))" && echo OK
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/dev/yolo/yolo-servicemonitor.yaml')))" && echo OK
```

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/dev/yolo/yolo-service.yaml infra/k8s/dev/yolo/yolo-servicemonitor.yaml
git commit -m "k8s(dev): label yolo-svc, name its port, add ServiceMonitor"
```

---

### Task 14: Dev `agent-svc` — add label + named port, add ServiceMonitor

**Files:**
- Modify: `infra/k8s/dev/agent/agent-service.yaml`
- Create: `infra/k8s/dev/agent/agent-servicemonitor.yaml`

- [ ] **Step 1: Edit the Service**

Replace the full content of `infra/k8s/dev/agent/agent-service.yaml` with:

```yaml
apiVersion: v1
kind: Service

metadata:
  name: agent-svc
  namespace: dev
  labels:
    app: agent

spec:
  type: ClusterIP

  selector:
    app: agent

  ports:
    - name: http
      protocol: TCP
      port: 8000
      targetPort: 8000
```

- [ ] **Step 2: Write the ServiceMonitor**

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: agent
  namespace: dev
spec:
  namespaceSelector:
    matchNames:
      - dev
  selector:
    matchLabels:
      app: agent
  endpoints:
    - port: http
      path: /metrics
      interval: 15s
```

Note: this mirrors the `agent-svc:8000` scrape target from the raw `prometheus-configmap.yaml` this replaces. `agent` does not currently expose `/metrics` (confirmed — no `prometheus_client`/`Instrumentator` usage found in `services/agent`), so this target will show as down in Prometheus. That's a pre-existing gap carried over for parity, not a regression introduced here; instrumenting the agent app is out of scope for this task.

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/dev/agent/agent-service.yaml')))" && echo OK
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/dev/agent/agent-servicemonitor.yaml')))" && echo OK
```

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/dev/agent/agent-service.yaml infra/k8s/dev/agent/agent-servicemonitor.yaml
git commit -m "k8s(dev): label agent-svc, name its port, add ServiceMonitor"
```

---

### Task 15: Prod `yolo-svc` — add label + named port, add ServiceMonitor (mirrors Task 13)

**Files:**
- Modify: `infra/k8s/prod/yolo/yolo-service.yaml`
- Create: `infra/k8s/prod/yolo/yolo-servicemonitor.yaml`

- [ ] **Step 1: Edit the Service**

Replace the full content of `infra/k8s/prod/yolo/yolo-service.yaml` with:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: yolo-svc
  namespace: prod
  labels:
    app: yolo
spec:
  selector:
    app: yolo
  ports:
    - name: http
      port: 8080
      targetPort: 8080
```

- [ ] **Step 2: Write the ServiceMonitor**

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: yolo
  namespace: prod
spec:
  namespaceSelector:
    matchNames:
      - prod
  selector:
    matchLabels:
      app: yolo
  endpoints:
    - port: http
      path: /metrics
      interval: 15s
```

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/prod/yolo/yolo-service.yaml')))" && echo OK
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/prod/yolo/yolo-servicemonitor.yaml')))" && echo OK
```

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/prod/yolo/yolo-service.yaml infra/k8s/prod/yolo/yolo-servicemonitor.yaml
git commit -m "k8s(prod): label yolo-svc, name its port, add ServiceMonitor"
```

---

### Task 16: Prod `agent-svc` — add label + named port, add ServiceMonitor (mirrors Task 14)

**Files:**
- Modify: `infra/k8s/prod/agent/agent-service.yaml`
- Create: `infra/k8s/prod/agent/agent-servicemonitor.yaml`

- [ ] **Step 1: Edit the Service**

Replace the full content of `infra/k8s/prod/agent/agent-service.yaml` with:

```yaml
apiVersion: v1
kind: Service

metadata:
  name: agent-svc
  namespace: prod
  labels:
    app: agent

spec:
  type: ClusterIP

  selector:
    app: agent

  ports:
    - name: http
      protocol: TCP
      port: 8000
      targetPort: 8000
```

- [ ] **Step 2: Write the ServiceMonitor**

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: agent
  namespace: prod
spec:
  namespaceSelector:
    matchNames:
      - prod
  selector:
    matchLabels:
      app: agent
  endpoints:
    - port: http
      path: /metrics
      interval: 15s
```

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/prod/agent/agent-service.yaml')))" && echo OK
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/prod/agent/agent-servicemonitor.yaml')))" && echo OK
```

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/prod/agent/agent-service.yaml infra/k8s/prod/agent/agent-servicemonitor.yaml
git commit -m "k8s(prod): label agent-svc, name its port, add ServiceMonitor"
```

---

### Task 17: Shared Grafana + Prometheus Ingress

**Files:**
- Create: `infra/k8s/common/monitoring/grafana-ingress.yaml`
- Create: `infra/k8s/common/monitoring/prometheus-ingress.yaml`

**Interfaces:**
- Consumes: Service names verified in Task 9 (`grafana`, `monitoring-prometheus`), namespace `monitoring` (created by the `monitoring` ArgoCD Application, Task 8).

- [ ] **Step 1: Write the Grafana Ingress**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: grafana-ingress
  namespace: monitoring
spec:
  ingressClassName: nginx
  rules:
    - host: moataz-grafana.fursa.click
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: grafana
                port:
                  number: 80
```

- [ ] **Step 2: Write the Prometheus Ingress**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: prometheus-ingress
  namespace: monitoring
spec:
  ingressClassName: nginx
  rules:
    - host: moataz-prometheus.fursa.click
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: monitoring-prometheus
                port:
                  number: 9090
```

**If Task 9's rendering showed different Service names, use those exact names here instead of `grafana` / `monitoring-prometheus`.**

Note: this file lives under `infra/k8s/common/monitoring/`, synced by the existing `cluster-resources` Application (`prune: true`, `selfHeal: true`, no fixed sync-wave ordering against the separate `monitoring` Application from Task 8). On the very first bootstrap, `cluster-resources` could sync this Ingress before the `monitoring` Application has created the `monitoring` namespace, causing one transient sync failure for these two objects; ArgoCD's `selfHeal` automatically retries and resolves it within its normal reconciliation loop — no manual step required.

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/common/monitoring/grafana-ingress.yaml')))" && echo OK
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/common/monitoring/prometheus-ingress.yaml')))" && echo OK
```

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/common/monitoring/grafana-ingress.yaml infra/k8s/common/monitoring/prometheus-ingress.yaml
git commit -m "k8s(common): add shared grafana and prometheus ingress"
```

---

### Task 18: Migrate the Agent Grafana dashboard with namespace filtering

**Files:**
- Create: `infra/k8s/common/monitoring/grafana-agent-dashboard.yaml`
- Delete: `infra/grafana/dashboards/agent.json`

**Interfaces:**
- Consumes: `infra/grafana/dashboards/agent.json` (source content), `grafana.sidecar.dashboards.label`/`labelValue` from Task 8 (`grafana_dashboard: "1"`).

- [ ] **Step 1: Read the current dashboard JSON**

```bash
cat infra/grafana/dashboards/agent.json
```

- [ ] **Step 2: Apply exactly these two transformations to the JSON**

**(a) Add a `namespace` template variable.** Replace:
```json
  "templating": {
    "list": []
  },
```
with:
```json
  "templating": {
    "list": [
      {
        "current": {
          "text": "All",
          "value": "$__all"
        },
        "datasource": {
          "type": "prometheus",
          "uid": "ffs4eqo9ka51cb"
        },
        "includeAll": true,
        "label": "Namespace",
        "multi": true,
        "name": "namespace",
        "options": [],
        "query": {
          "query": "label_values(agent_chat_requests_total, namespace)",
          "refId": "PrometheusVariableQueryEditor-VariableQuery"
        },
        "refresh": 2,
        "regex": "",
        "type": "query"
      }
    ]
  },
```

**(b) Add a `namespace=~"$namespace"` label filter to every PromQL `expr` string.** Five `expr` values change:

| Panel | Old `expr` selector | New `expr` selector |
|---|---|---|
| "Chat Error Rate" | `agent_chat_requests_total{status="error"}` and `agent_chat_requests_total` (bare) | `agent_chat_requests_total{status="error", namespace=~"$namespace"}` and `agent_chat_requests_total{namespace=~"$namespace"}` |
| "Request Latency Percentiles" (×3 targets: p50/p95/p99) | `agent_chat_request_duration_seconds_bucket` (bare) | `agent_chat_request_duration_seconds_bucket{namespace=~"$namespace"}` |
| "Chat Requests per Minute" | `agent_chat_requests_total` (bare) | `agent_chat_requests_total{namespace=~"$namespace"}` |
| "Input / Output Tokens" (×2 targets) | `agent_input_tokens_total` / `agent_output_tokens_total` (bare) | `agent_input_tokens_total{namespace=~"$namespace"}` / `agent_output_tokens_total{namespace=~"$namespace"}` |

Concretely, the "Chat Error Rate" panel's `expr` becomes:
```
100 *
sum(rate(agent_chat_requests_total{status=\"error\", namespace=~\"$namespace\"}[5m]))
/
clamp_min(
  sum(rate(agent_chat_requests_total{namespace=~\"$namespace\"}[5m])),
  0.000001
)
```

(Note: these panels reference metrics — `agent_chat_requests_total`, `agent_chat_request_duration_seconds_bucket`, `agent_input_tokens_total`, `agent_output_tokens_total` — that are not currently emitted anywhere in `services/agent` (confirmed by grep). The dashboard was already non-functional for these panels before this migration; adding the namespace filter doesn't change that, and instrumenting the agent app is out of scope. Once/if the agent emits these metrics, the namespace filter will already be correct.)

- [ ] **Step 3: Wrap the transformed JSON in a ConfigMap**

Create `infra/k8s/common/monitoring/grafana-agent-dashboard.yaml`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-agent-dashboard
  namespace: monitoring
  labels:
    grafana_dashboard: "1"
  annotations:
    grafana_folder: "Agent"
data:
  agent.json: |
    <the full transformed JSON from Step 2, indented 4 spaces to nest under this key>
```

The `grafana_dashboard: "1"` label must exactly match `grafana.sidecar.dashboards.label: grafana_dashboard` / `labelValue: "1"` from Task 8's Helm values — that match is what makes the sidecar pick this ConfigMap up. `grafana_folder: "Agent"` preserves the original dashboard-provider folder grouping from the manifest this replaces.

- [ ] **Step 4: Delete the original file**

```bash
git rm infra/grafana/dashboards/agent.json
```

- [ ] **Step 5: Validate**

```bash
python3 -c "
import yaml, json
doc = yaml.safe_load(open('infra/k8s/common/monitoring/grafana-agent-dashboard.yaml'))
assert doc['metadata']['labels']['grafana_dashboard'] == '1'
parsed = json.loads(doc['data']['agent.json'])
assert parsed['templating']['list'][0]['name'] == 'namespace'
assert 'namespace=~\"\$namespace\"' in json.dumps(parsed)
print('OK')
"
```
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add infra/k8s/common/monitoring/grafana-agent-dashboard.yaml
git commit -m "k8s(common): migrate agent dashboard to sidecar-discovered ConfigMap with namespace filter"
```

---

### Task 19: Shared ArgoCD Ingress

**Files:**
- Create: `infra/k8s/common/argocd/argocd-ingress.yaml`

- [ ] **Step 1: Write the Ingress**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: argocd-ingress
  namespace: argocd
spec:
  ingressClassName: nginx
  rules:
    - host: moataz-argocd.fursa.click
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: argocd-server
                port:
                  number: 80
```

Lives once under `infra/k8s/common/` (synced by `cluster-resources`), not duplicated under `dev`/`prod` — there's exactly one ArgoCD control plane, not one per environment, and an Ingress object can't target a Service in a different namespace, so duplicating this into `dev`/`prod` (each would still need `namespace: argocd` explicitly) would add nothing.

- [ ] **Step 2: Validate YAML syntax**

```bash
python3 -c "import yaml; list(yaml.safe_load_all(open('infra/k8s/common/argocd/argocd-ingress.yaml')))" && echo OK
```

- [ ] **Step 3: Commit**

```bash
git add infra/k8s/common/argocd/argocd-ingress.yaml
git commit -m "k8s(common): add shared argocd ingress"
```

---

### Task 20: Remove the old per-env raw Grafana/Prometheus manifests

**Files:**
- Delete: `infra/k8s/dev/grafana/`, `infra/k8s/dev/prometheus/`
- Delete: `infra/k8s/prod/grafana/`, `infra/k8s/prod/prometheus/`

These are replaced end-to-end by the shared `monitoring` release (Task 8) and its Ingress (Task 17). The repo currently has duplicated raw manifests per environment (not an existing intentional shared stack — confirmed during design by inspecting `infra/k8s/{dev,prod}/{grafana,prometheus}/*`), so per direction they're removed rather than kept alongside the new stack.

- [ ] **Step 1: Delete the directories**

```bash
git rm -r infra/k8s/dev/grafana infra/k8s/dev/prometheus
git rm -r infra/k8s/prod/grafana infra/k8s/prod/prometheus
```

- [ ] **Step 2: Confirm nothing else references the deleted PVCs/ConfigMaps/Services**

```bash
grep -rn "grafana-svc\|prometheus-svc\|grafana-pvc\|prometheus-pvc\|prometheus-config\b" infra/ scripts/ .github/ 2>/dev/null
```
Expected: no output (nothing outside the deleted directories referenced these names — the new Ingress resources in Task 17 reference `grafana` / `monitoring-prometheus`, not `grafana-svc` / `prometheus-svc`).

- [ ] **Step 3: Commit**

```bash
git commit -m "k8s: remove per-env grafana/prometheus manifests, replaced by shared monitoring stack"
```

---

### Task 21: Wire `NEXT_PUBLIC_AGENT_URL=/api` through the frontend Docker build

**Files:**
- Modify: `.github/workflows/cd.yml`

**Interfaces:**
- Consumes: existing `ARG NEXT_PUBLIC_AGENT_URL` / `ENV NEXT_PUBLIC_AGENT_URL=$NEXT_PUBLIC_AGENT_URL` in `services/frontend/Dockerfile` (already present, no change needed there) and `process.env.NEXT_PUBLIC_AGENT_URL` in `services/frontend/lib/api.ts` (already present, no change needed there).

- [ ] **Step 1: Add `build-args` to the frontend matrix entry**

In `.github/workflows/cd.yml`, the `build` job's matrix currently reads:

```yaml
    strategy:
      fail-fast: false
      matrix:
        include:
          - service: frontend
            changed: ${{ needs.detect.outputs.frontend }}
            context: services/frontend
            image: moataz189/frontend-service
```

Change the `frontend` entry to:

```yaml
    strategy:
      fail-fast: false
      matrix:
        include:
          - service: frontend
            changed: ${{ needs.detect.outputs.frontend }}
            context: services/frontend
            image: moataz189/frontend-service
            build-args: NEXT_PUBLIC_AGENT_URL=/api
```

(The other three matrix entries — `agent`, `yolo`, `img-proc-mcp` — are left untouched; they simply won't have a `build-args` key, which evaluates to an empty string wherever referenced below.)

- [ ] **Step 2: Pass it into the build step**

The `Build and push image` step currently reads:

```yaml
      - name: Build and push image
        if: matrix.changed == 'true'
        uses: docker/build-push-action@v6
        with:
          context: ${{ matrix.context }}
          push: true
          platforms: linux/amd64
          tags: ${{ matrix.image }}:${{ github.sha }}
```

Add a `build-args` line:

```yaml
      - name: Build and push image
        if: matrix.changed == 'true'
        uses: docker/build-push-action@v6
        with:
          context: ${{ matrix.context }}
          push: true
          platforms: linux/amd64
          tags: ${{ matrix.image }}:${{ github.sha }}
          build-args: ${{ matrix.build-args }}
```

A relative `/api` resolves against whatever origin serves the page, so the same built image works unmodified on both `moataz-dev.fursa.click` and `moataz-prod.fursa.click` — no per-environment image needed.

**Known follow-up (not fixed by this task, noted for the user):** this workflow only rebuilds an image when `services/frontend/**` actually changes (via `dorny/paths-filter` comparing `github.event.before`..`HEAD`). Since this task only touches `cd.yml` itself, not frontend source, merging this change alone will not trigger an immediate rebuild — the currently-deployed frontend image keeps its old (broken) default until the next real `services/frontend/**` change, or until someone manually re-runs a frontend build with this fix in place.

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/cd.yml'))" && echo OK
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/cd.yml
git commit -m "cd: wire NEXT_PUBLIC_AGENT_URL=/api into the frontend docker build"
```

---

### Task 22: Full validation pass

**Files:** none (validation only)

- [ ] **Step 1: Re-run Terraform validation end-to-end**

```bash
cd infra/tf
terraform fmt -check -recursive
terraform validate
```
Expected: `fmt -check` prints nothing; `validate` prints `Success! The configuration is valid.`

- [ ] **Step 2: YAML-syntax-check every new/modified manifest in one pass**

```bash
cd /Users/moatazodeh/Documents/polyaifursa
find infra/k8s infra/argocd infra/helm -name "*.yaml" -newer infra/tf/.terraform.lock.hcl -print0 \
  | xargs -0 -I{} python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1]))); print('OK', sys.argv[1])" {}
```
Expected: `OK <path>` for every file this plan touched, no tracebacks.

- [ ] **Step 3: Spec-coverage self-check**

Confirm each spec requirement maps to a completed task:
- Terraform ALB/ACM/Route53/SG/ASG attachment → Tasks 1–6 ✓
- ingress-nginx pinned Helm chart, fixed NodePorts, DaemonSet+Local → Task 7 ✓
- Shared kube-prometheus-stack, explicit ServiceMonitor discovery config → Task 8 ✓
- Deterministic Prometheus Service name, verified not assumed → Task 9 ✓
- ServiceMonitors for dev+prod, named ports, matching labels → Tasks 13–16 ✓
- Grafana dashboard migration with namespace filtering, sidecar label match → Task 18 ✓
- Frontend/agent Ingress dev+prod, path-split, same host → Tasks 11–12 ✓
- Shared Grafana/Prometheus/ArgoCD Ingress → Tasks 17, 19 ✓
- Old per-env grafana/prometheus manifests removed → Task 20 ✓
- `NEXT_PUBLIC_AGENT_URL=/api` wired through the Docker build → Task 21 ✓
- ArgoCD insecure mode for plain-HTTP ingress → Task 10 ✓

- [ ] **Step 4: Record the required post-deploy checks for whoever runs the actual provisioning workflow**

These cannot be executed now (no live cluster/ALB exists yet in this session) — write them down so they run once the infrastructure is actually provisioned out-of-band:

```bash
# 1. ALB health-check assumption (required by the design corrections — verify, don't assume):
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: unmatched.invalid" http://<any-worker-public-ip>:30080/
# Expected: 404, consistently, across repeated calls.

# 2. ingress-nginx is a DaemonSet with one pod per worker node:
kubectl get pods -n ingress-nginx -o wide
kubectl get nodes

# 3. Fixed NodePorts:
kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.spec.ports}'
# Expected: 30080 (http) and 30443 (https).

# 4. ALB target group: all worker ASG instances healthy.
aws elbv2 describe-target-health --target-group-arn <target_group_arn from terraform output>

# 5. Each of the 5 hostnames resolves through the ALB and serves the right backend:
for h in moataz-dev moataz-prod moataz-grafana moataz-prometheus moataz-argocd; do
  echo "== $h.fursa.click =="
  curl -sI "https://$h.fursa.click/" | head -1
done

# 6. Prometheus Service name matches what Task 17's Ingress uses:
kubectl get svc -n monitoring monitoring-prometheus grafana

# 7. ServiceMonitors picked up, targets visible in both namespaces:
kubectl get servicemonitors -A
# then check the Targets page at https://moataz-prometheus.fursa.click/targets for "yolo" (dev and prod)

# 8. Grafana sidecar picked up the migrated dashboard (UI: Dashboards -> Agent folder).

# 9. No kubectl port-forward needed for any of: frontend, agent, grafana, prometheus, argocd.

# 10. terraform destroy in infra/tf, followed by re-running the cluster.yaml provisioning
#     workflow and the normal ArgoCD bootstrap, recreates everything above with zero manual steps.
```

No commit for this task — it's the validation record, not code.

---

### Task 23: Git workflow — merge to `dev`, return, push, open PR to `main`

**Files:** none (git operations only)

- [ ] **Step 1: Confirm current branch and clean tree**

```bash
git status
git branch --show-current
```
Expected: `feature/expose-cluster-ingress`, clean working tree (everything committed in Tasks 1–21).

- [ ] **Step 2: Regular merge into `dev` (no PR)**

```bash
git fetch origin dev
git checkout dev
git merge --no-ff feature/expose-cluster-ingress -m "Merge feature/expose-cluster-ingress into dev: expose cluster via ALB/ingress-nginx, shared monitoring stack"
```

- [ ] **Step 3: Return to the feature branch**

```bash
git checkout feature/expose-cluster-ingress
```

- [ ] **Step 4: Push the feature branch**

```bash
git push -u origin feature/expose-cluster-ingress
```

- [ ] **Step 5: Push `dev`**

The instructions say to merge into `dev` locally; publishing that merge requires pushing `dev` too — without this the local merge never reaches the shared branch:

```bash
git push origin dev
```

- [ ] **Step 6: Open the PR to `main`**

```bash
gh pr create \
  --base main \
  --head feature/expose-cluster-ingress \
  --title "Expose the cluster to the internet (ALB + ingress-nginx + shared monitoring)" \
  --body "$(cat <<'EOF'
## Summary
- Terraform `modules/ingress`: ALB with HTTPS:443 listener (ACM cert), target group forwarding to ingress-nginx's fixed NodePort 30080, worker ASG attachment, 5 Route53 alias records in the data-sourced `fursa.click` zone.
- ingress-nginx installed via a version-pinned ArgoCD Helm Application (chart 4.14.1), DaemonSet + `externalTrafficPolicy: Local`, fixed NodePorts 30080/30443.
- Shared `kube-prometheus-stack` (chart 88.0.1) via ArgoCD Helm Application in the `monitoring` namespace, replacing the old per-env raw Grafana/Prometheus manifests; ServiceMonitors for `yolo`/`agent` in both `dev` and `prod`; migrated Agent dashboard with a namespace filter.
- Path-split Ingress (`/` and `/api`) for frontend/agent in both `dev` and `prod`; shared Ingress for Grafana, Prometheus, and ArgoCD.
- `NEXT_PUBLIC_AGENT_URL=/api` wired through the frontend Docker build args in `cd.yml`.
- ArgoCD server patched to insecure (HTTP) mode in `scripts/bootstrap.sh`, required for the plain-HTTP ingress path to reach it.

Full design rationale: `docs/superpowers/specs/2026-08-02-expose-cluster-ingress-design.md`
Implementation plan: `docs/superpowers/plans/2026-08-02-expose-cluster-ingress.md`

## Test plan
- [x] `terraform fmt -check -recursive` / `terraform validate` clean
- [x] `terraform plan` against real state shows only additive changes, no existing resource touched
- [x] Both Helm charts rendered locally with our values; DaemonSet/NodePort/ExternalTrafficPolicy and exact Prometheus/Grafana Service names confirmed
- [x] All new/modified YAML syntax-checked
- [ ] Post-provisioning checks (ALB target health, 404 health-check verification, hostname reachability, ServiceMonitor targets, no port-forward needed) — see Task 22 of the implementation plan; run once the infrastructure is actually provisioned

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 7: Report the PR URL back to the user**

No further commit for this task.

---

## Self-Review Notes

- **Spec coverage:** every numbered requirement in the design spec (§4 Terraform, §5 ingress-nginx, §6 Ingress resources, §7 monitoring stack, §8 frontend wiring, §10 validation, §11 git workflow) maps to at least one task above (cross-checked explicitly in Task 22 Step 3).
- **Placeholder scan:** no TBD/TODO; the one place content is described as a diff table rather than fully inlined twice (Task 18's dashboard JSON transformation) gives exact old→new strings for every change, not a vague instruction — chosen because the source JSON is ~460 lines and this plan is executed by the same session that wrote it, not handed to a blind fresh engineer.
- **Type/name consistency:** `worker_security_group_id` (Task 1) matches the variable name consumed in Task 2/3/5; `monitoring-prometheus`/`grafana` Service names are identical between Task 8 (values that produce them), Task 9 (verification), and Task 17 (Ingress backends); `app: yolo`/`app: agent` labels and the `http` port name are identical across the Service edits (Tasks 13–16) and their ServiceMonitors; all 5 hostnames are copied verbatim from the Global Constraints section into Tasks 5, 11, 12, 17, 19.

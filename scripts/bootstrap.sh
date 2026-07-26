#!/bin/bash
set -euo pipefail

export KUBECONFIG=/etc/kubernetes/admin.conf

CALICO_VERSION="v3.32.1"
ARGOCD_NAMESPACE="argocd"
GIT_REPO_URL=https://github.com/moataz189/polyaifursa.git

echo "======================================"
echo "Starting Kubernetes bootstrap..."
echo "======================================"

#################################################
# Calico
#################################################

echo "Installing Calico..."

kubectl apply -f \
"https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"

echo "Waiting for nodes to become Ready..."

kubectl wait \
  --for=condition=Ready \
  nodes \
  --all \
  --timeout=10m

#################################################
# Metrics Server
#################################################

echo "Installing Metrics Server..."

kubectl apply -f \
https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml


echo "Patching Metrics Server..."
kubectl patch deployment metrics-server -n kube-system \
  --type='json' \
  -p='[
    {
      "op":"add",
      "path":"/spec/template/spec/containers/0/args/-",
      "value":"--kubelet-insecure-tls"
    }
  ]'

echo "Waiting for Metrics Server..."


kubectl rollout status \
deployment/metrics-server \
-n kube-system \
--timeout=10m

#################################################
# AWS EBS CSI Driver
#################################################

echo "Installing AWS EBS CSI Driver..."

#################################################
# AWS EBS CSI Driver
#################################################

EBS_CSI_VERSION="release-1.63"

echo "Installing AWS EBS CSI Driver..."

kubectl apply -k \
  "github.com/kubernetes-sigs/aws-ebs-csi-driver/deploy/kubernetes/overlays/stable/?ref=${EBS_CSI_VERSION}"

echo "Waiting for EBS CSI controller..."

kubectl rollout status \
  deployment/ebs-csi-controller \
  --namespace kube-system \
  --timeout=10m

echo "Waiting for EBS CSI node pods..."

kubectl rollout status \
  daemonset/ebs-csi-node \
  --namespace kube-system \
  --timeout=10m

echo "AWS EBS CSI Driver installed successfully."

#################################################
# ArgoCD
#################################################

echo "Creating ArgoCD namespace..."

kubectl create namespace "${ARGOCD_NAMESPACE}" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

echo "Installing ArgoCD..."

kubectl create namespace argocd
kubectl apply -n argocd --server-side --force-conflicts -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

echo "Waiting for ArgoCD components..."

kubectl rollout status \
deployment/argocd-server \
--namespace argocd \
--timeout=10m

kubectl rollout status \
deployment/argocd-repo-server \
--namespace argocd \
--timeout=10m

kubectl rollout status \
deployment/argocd-applicationset-controller \
--namespace argocd \
--timeout=10m

#################################################
# App of Apps
#################################################

echo "App of Apps installation will be added next..."
#################################################
# App of Apps
#################################################

echo "Deploying App of Apps..."


kubectl apply -f /home/ubuntu/app-of-apps.yaml

echo "App of Apps deployed successfully."
echo
echo "======================================"
echo "Bootstrap completed successfully!"
echo "Repository: ${GIT_REPO_URL}"
echo "======================================"
#!/bin/bash
set -euo pipefail

REGION="${region}"
JOIN_PARAMETER_NAME="/moataz-k8s/join-command"
#CALICO_VERSION="v3.32.1"

echo "Starting Kubernetes control-plane initialization..."

# Start the container runtime and kubelet
systemctl enable --now crio
systemctl enable --now kubelet
aws ssm delete-parameter \
  --name "$${JOIN_PARAMETER_NAME}" \
  --region "$${REGION}" \
  2>/dev/null || true
# Initialize the Kubernetes control plane
kubeadm init \
  --pod-network-cidr=192.168.0.0/16 \
  --cri-socket=unix:///var/run/crio/crio.sock

export KUBECONFIG=/etc/kubernetes/admin.conf

# Configure kubectl for the ubuntu user
mkdir -p /home/ubuntu/.kube
cp /etc/kubernetes/admin.conf /home/ubuntu/.kube/config
chown -R ubuntu:ubuntu /home/ubuntu/.kube

#echo "Installing Calico CNI..."

#kubectl apply -f \
 # "https://raw.githubusercontent.com/projectcalico/calico/$${CALICO_VERSION}/manifests/calico.yaml"

#echo "Waiting for Kubernetes system pods..."

##kubectl wait \
#  --namespace kube-system \
#  --for=condition=Ready \
#  pods \
#  --all \
#  --timeout=10m

# Generate the full command that workers will use to join
JOIN_COMMAND=$(kubeadm token create \
  --ttl 0 \
  --print-join-command)

# Add the CRI-O socket to the join command
JOIN_COMMAND="$${JOIN_COMMAND} --cri-socket=unix:///var/run/crio/crio.sock"

echo "Saving worker join command in AWS SSM Parameter Store..."

aws ssm put-parameter \
  --name "$${JOIN_PARAMETER_NAME}" \
  --description "Kubernetes worker join command" \
  --type "SecureString" \
  --value "$${JOIN_COMMAND}" \
  --overwrite \
  --region "$${REGION}"

echo "Control plane initialized successfully."
##echo "Calico installed successfully."
echo "Worker join command saved in SSM."
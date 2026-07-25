#!/usr/bin/env bash

set -euo pipefail

KUBERNETES_VERSION="v1.35"
CRIO_VERSION="v1.35"

# Save installation logs inside the AMI
exec > >(tee /var/log/packer-k8s-install.log) 2>&1

echo "Starting Kubernetes dependencies installation..."

# Update the operating system and install basic packages
apt-get update

DEBIAN_FRONTEND=noninteractive apt-get install -y \
  apt-transport-https \
  ca-certificates \
  curl \
  gpg \
  jq \
  unzip \
  ebtables \
  ethtool \
  software-properties-common

# Disable swap
echo "Disabling swap..."

swapoff -a

# Disable configured swap entries after reboot
sed -i '/\sswap\s/s/^/#/' /etc/fstab

# Load kernel modules required by Kubernetes networking
echo "Configuring kernel modules..."

cat <<EOF >/etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF

modprobe overlay
modprobe br_netfilter

# Configure network parameters
echo "Configuring kernel network parameters..."

cat <<EOF >/etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF

sysctl --system

# Create the directory used for repository keys
install -m 0755 -d /etc/apt/keyrings

# Install AWS CLI
echo "Installing AWS CLI..."

curl -fsSL \
  "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" \
  -o /tmp/awscliv2.zip

unzip -q /tmp/awscliv2.zip -d /tmp

/tmp/aws/install --update

rm -rf /tmp/aws /tmp/awscliv2.zip

# Add Kubernetes repository
echo "Adding Kubernetes ${KUBERNETES_VERSION} repository..."

curl -fsSL \
  "https://pkgs.k8s.io/core:/stable:/${KUBERNETES_VERSION}/deb/Release.key" \
  | gpg --dearmor \
  -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg

chmod 644 /etc/apt/keyrings/kubernetes-apt-keyring.gpg

cat <<EOF >/etc/apt/sources.list.d/kubernetes.list
deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/${KUBERNETES_VERSION}/deb/ /
EOF

chmod 644 /etc/apt/sources.list.d/kubernetes.list

# Add the stable CRI-O repository
echo "Adding CRI-O ${CRIO_VERSION} repository..."

curl -fsSL \
  "https://download.opensuse.org/repositories/isv:/cri-o:/stable:/${CRIO_VERSION}/deb/Release.key" \
  | gpg --dearmor \
  -o /etc/apt/keyrings/cri-o-apt-keyring.gpg

chmod 644 /etc/apt/keyrings/cri-o-apt-keyring.gpg

cat <<EOF >/etc/apt/sources.list.d/cri-o.list
deb [signed-by=/etc/apt/keyrings/cri-o-apt-keyring.gpg] https://download.opensuse.org/repositories/isv:/cri-o:/stable:/${CRIO_VERSION}/deb/ /
EOF

chmod 644 /etc/apt/sources.list.d/cri-o.list

# Install CRI-O and Kubernetes tools
echo "Installing CRI-O and Kubernetes components..."

apt-get update

DEBIAN_FRONTEND=noninteractive apt-get install -y \
  cri-o \
  kubelet \
  kubeadm \
  kubectl \
  amazon-ecr-credential-helper

# Prevent accidental Kubernetes upgrades
apt-mark hold kubelet kubeadm kubectl

# Enable services
echo "Enabling CRI-O and kubelet..."

systemctl daemon-reload
systemctl enable crio.service
systemctl enable kubelet.service

# CRI-O can run immediately.
# kubelet may restart until kubeadm init or kubeadm join is executed.
systemctl start crio.service

# Verify the installation
echo "Installed versions:"

crio --version
kubeadm version
kubelet --version
kubectl version --client

# Clean temporary package data before creating the AMI
echo "Cleaning the AMI..."

apt-get clean
rm -rf /var/lib/apt/lists/*
rm -rf /tmp/*

echo "Kubernetes dependencies installation completed at $(date)" \
  | tee /var/log/k8s-setup-complete
packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.3.0"
    }
  }
}

variable "region" {
  description = "AWS region in which the AMI will be built"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "Temporary EC2 instance type used during the Packer build"
  type        = string
  default     = "t3.medium"
}

locals {
  ami_timestamp = formatdate("YYYYMMDD-hhmmss", timestamp())
}

source "amazon-ebs" "k8s_node" {
  region        = var.region
  instance_type = var.instance_type
  ssh_username  = "ubuntu"

  ami_name        = "moataz-k8s-node-${local.ami_timestamp}"
  ami_description = "Ubuntu 22.04 Kubernetes node with CRI-O, kubelet, kubeadm and kubectl"

  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
      architecture        = "x86_64"
    }

    owners      = ["099720109477"]
    most_recent = true
  }
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 20
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  tags = {
    Name      = "k8s-node-base"
    Project   = "moataz-polyai"
    ManagedBy = "Packer"
  }

  run_tags = {
    Name      = "packer-k8s-node-builder"
    Project   = "moataz-polyai"
    ManagedBy = "Packer"
  }
}

build {
  name = "k8s-node"

  sources = [
    "source.amazon-ebs.k8s_node"
  ]

  provisioner "shell" {
    script          = "${path.root}/install-k8s-deps.sh"
    execute_command = "chmod +x {{ .Path }}; sudo -E {{ .Path }}"
  }
}
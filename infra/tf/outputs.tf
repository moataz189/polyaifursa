# ==========================================
# VPC
# ==========================================

output "vpc_id" {
  description = "ID of the VPC"
  value       = module.vpc.vpc_id
}

output "vpc_name" {
  description = "Name of the VPC"
  value       = module.vpc.name
}

# ==========================================
# Public Subnets
# ==========================================

output "public_subnet_ids" {
  description = "IDs of the public subnets"
  value       = module.vpc.public_subnets
}

output "public_subnet_cidr_blocks" {
  description = "CIDR blocks of the public subnets"
  value       = module.vpc.public_subnets_cidr_blocks
}

# ==========================================
# Kubernetes Control Plane
# ==========================================

output "control_plane_instance_id" {
  description = "EC2 instance ID of the Kubernetes control plane"
  value       = module.k8s_cluster.control_plane_instance_id
}

output "control_plane_instance_name" {
  description = "Name of the Kubernetes control plane instance"
  value       = module.k8s_cluster.control_plane_instance_name
}

output "control_plane_public_ip" {
  description = "Public IP address of the Kubernetes control plane"
  value       = module.k8s_cluster.control_plane_public_ip
}

output "control_plane_private_ip" {
  description = "Private IP address of the Kubernetes control plane"
  value       = module.k8s_cluster.control_plane_private_ip
}

# ==========================================
# Kubernetes Workers
# ==========================================

output "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group"
  value       = module.k8s_cluster.worker_asg_name
}

output "worker_launch_template_id" {
  description = "ID of the worker Launch Template"
  value       = module.k8s_cluster.worker_launch_template_id
}

output "worker_launch_template_name" {
  description = "Name of the worker Launch Template"
  value       = module.k8s_cluster.worker_launch_template_name
}
output "worker_instance_ids" {
  value = module.k8s_cluster.worker_instance_ids
}

output "worker_private_ips" {
  value = module.k8s_cluster.worker_private_ips
}

output "worker_public_ips" {
  value = module.k8s_cluster.worker_public_ips
}

# ==========================================
# Kubernetes AMI
# ==========================================

output "k8s_ami_id" {
  description = "AMI ID selected for Kubernetes nodes"
  value       = data.aws_ami.k8s_node.id
}

output "k8s_ami_name" {
  description = "AMI name selected for Kubernetes nodes"
  value       = data.aws_ami.k8s_node.name
}
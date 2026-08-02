output "control_plane_public_ip" {
  description = "Public IP address of the Kubernetes control plane"
  value       = aws_instance.control_plane.public_ip
}

output "control_plane_private_ip" {
  description = "Private IP address of the Kubernetes control plane"
  value       = aws_instance.control_plane.private_ip
}

output "control_plane_instance_id" {
  description = "EC2 instance ID of the Kubernetes control plane"
  value       = aws_instance.control_plane.id
}

output "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group"
  value       = aws_autoscaling_group.workers.name
}

output "worker_launch_template_id" {
  description = "ID of the worker Launch Template"
  value       = aws_launch_template.workers.id
}
output "control_plane_instance_name" {
  description = "Name tag of the Kubernetes control plane instance"
  value       = aws_instance.control_plane.tags["Name"]
}
output "worker_launch_template_name" {
  description = "Name of the worker Launch Template"
  value       = aws_launch_template.workers.name
}
output "worker_instance_ids" {
  description = "EC2 instance IDs of Kubernetes workers"
  value       = data.aws_instances.workers.ids
}

output "worker_private_ips" {
  description = "Private IP addresses of Kubernetes workers"
  value       = data.aws_instances.workers.private_ips
}

output "worker_public_ips" {
  description = "Public IP addresses of Kubernetes workers"
  value       = data.aws_instances.workers.public_ips
}

output "worker_security_group_id" {
  description = "ID of the worker nodes security group"
  value       = aws_security_group.workers.id
}
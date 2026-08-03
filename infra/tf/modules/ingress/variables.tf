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

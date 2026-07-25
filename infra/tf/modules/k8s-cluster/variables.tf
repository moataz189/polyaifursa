variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "vpc_id" {
  description = "ID of the VPC"
  type        = string
}

variable "subnet_ids" {
  description = "IDs of the public subnets"
  type        = list(string)
}

variable "key_name" {
  description = "Existing EC2 Key Pair name"
  type        = string
}

variable "worker_min_size" {
  description = "Minimum number of worker nodes"
  type        = number
}

variable "worker_max_size" {
  description = "Maximum number of worker nodes"
  type        = number
}

variable "worker_desired_capacity" {
  description = "Desired number of worker nodes"
  type        = number
}
variable "vpc_cidr" {
  description = "CIDR block of the VPC"
  type        = string
}
variable "ami_id" {
  description = "AMI ID used for Kubernetes nodes"
  type        = string
}
variable "region" {
  description = "AWS region in which the cluster is deployed"
  type        = string
}
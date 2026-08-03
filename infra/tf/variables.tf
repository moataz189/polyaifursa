variable "region" {
  description = "AWS region where the cluster will be created"
  type        = string
}

variable "project_name" {
  description = "Project name used in AWS resource names"
  type        = string
  default     = "polyai"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
}



variable "key_name" {
  description = "Existing AWS EC2 key pair name"
  type        = string
}

variable "worker_desired_capacity" {
  description = "Desired number of worker nodes"
  type        = number
  default     = 1
}

variable "worker_min_size" {
  description = "Minimum number of worker nodes"
  type        = number
  default     = 1
}

variable "worker_max_size" {
  description = "Maximum number of worker nodes"
  type        = number
  default     = 3
}

variable "route53_zone_name" {
  description = "Name of the existing Route53 hosted zone used for public DNS records"
  type        = string
}

variable "s3_bucket_name" {
  description = "Name of the existing (externally managed) S3 bucket used for image storage"
  type        = string
}
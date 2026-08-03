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
variable "s3_bucket_name" {
  description = "Name of the existing (externally managed) S3 bucket the agent/yolo/img-proc-mcp services read and write images to"
  type        = string
}
variable "bedrock_model_ids" {
  description = "Bedrock foundation model IDs the agent service is allowed to invoke. Must match services/agent/app.py's ALLOWED_MODELS set."
  type        = list(string)
  default = [
    "anthropic.claude-3-haiku-20240307-v1:0",
    "amazon.nova-micro-v1:0",
    "amazon.nova-lite-v1:0",
    "openai.gpt-oss-20b-1:0",
    "meta.llama3-1-8b-instruct-v1:0",
    "mistral.mistral-7b-instruct-v0:2",
  ]
}
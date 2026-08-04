variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "alert_email" {
  description = "Email address that receives Alertmanager notifications via SNS"
  type        = string
  sensitive   = true
}

output "topic_arn" {
  description = "ARN of the SNS topic Alertmanager publishes to"
  value       = aws_sns_topic.alerts.arn
}

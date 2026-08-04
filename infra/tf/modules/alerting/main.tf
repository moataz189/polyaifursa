resource "aws_sns_topic" "alerts" {
  # Deterministic name -- an SNS topic ARN is derived from region + account
  # id + this name, with no random suffix, so the same name always yields
  # the same ARN in this account/region even after a full terraform destroy
  # + re-apply. That determinism is what makes the values.yaml render step
  # (infra/k8s/monitoring/values.yaml.tpl, rendered by .github/workflows/
  # cluster.yaml) safe to treat as reproducible.
  name = "${var.project_name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

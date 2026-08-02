output "alb_dns_name" {
  description = "DNS name of the ingress ALB"
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "Route53 hosted zone ID of the ALB (for alias records)"
  value       = aws_lb.this.zone_id
}

output "target_group_arn" {
  description = "ARN of the ALB target group forwarding to the ingress-nginx NodePort"
  value       = aws_lb_target_group.http_node_port.arn
}

output "certificate_arn" {
  description = "ARN of the validated ACM certificate used by the HTTPS listener"
  value       = aws_acm_certificate_validation.this.certificate_arn
}

output "hostnames" {
  description = "Public hostnames routed through the ALB"
  value       = var.hostnames
}

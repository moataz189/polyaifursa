region       = "us-east-1"
project_name = "moataz-polyai-us-east-1-cluster"

vpc_cidr = "10.0.0.0/16"
key_name = "moataz-key"

worker_min_size         = 1
worker_max_size         = 3
worker_desired_capacity = 1

route53_zone_name = "fursa.click"
s3_bucket_name    = "moataz-polyai-images"

# TEMPORARY: hardcoded per explicit instruction for this implementation pass
# only. To be replaced with a GitHub Actions secret (TF_VAR_alert_email) so
# this value is never committed to git -- tracked as follow-up work, not
# done yet.
alert_email = "moataz.ody44@gmail.com"
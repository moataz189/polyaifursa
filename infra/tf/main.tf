terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket = "moataz-terraform-state-bucket-2026"
    key    = "k8s-cluster/terraform.tfstate"
    region = "us-east-1"

    # dynamodb_table = "terraform-locks"
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = terraform.workspace
      ManagedBy   = "Terraform"
    }
  }

}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  selected_azs = slice(
    data.aws_availability_zones.available.names,
    0,
    2
  )
}
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = "${var.project_name}-${terraform.workspace}-vpc"
  cidr = var.vpc_cidr

  azs = local.selected_azs
  public_subnets = [
    cidrsubnet(var.vpc_cidr, 4, 0),
    cidrsubnet(var.vpc_cidr, 4, 1)
  ]

  map_public_ip_on_launch = true

  enable_nat_gateway = false
  enable_vpn_gateway = false
}


data "aws_ami" "k8s_node" {
  most_recent = true
  owners      = ["self"]

  filter {
    name   = "name"
    values = ["moataz-k8s-node-*"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}
module "k8s_cluster" {
  source = "./modules/k8s-cluster"

  project_name            = var.project_name
  vpc_id                  = module.vpc.vpc_id
  subnet_ids              = module.vpc.public_subnets
  ami_id                  = data.aws_ami.k8s_node.id
  key_name                = var.key_name
  vpc_cidr                = var.vpc_cidr
  worker_min_size         = var.worker_min_size
  worker_max_size         = var.worker_max_size
  worker_desired_capacity = var.worker_desired_capacity
  region                  = var.region
}

locals {
  ingress_hostnames = [
    "moataz-dev.fursa.click",
    "moataz-prod.fursa.click",
    "moataz-grafana.fursa.click",
    "moataz-prometheus.fursa.click",
    "moataz-argocd.fursa.click",
  ]
}

module "ingress" {
  source = "./modules/ingress"

  project_name             = var.project_name
  vpc_id                   = module.vpc.vpc_id
  public_subnet_ids        = module.vpc.public_subnets
  worker_asg_name          = module.k8s_cluster.worker_asg_name
  worker_security_group_id = module.k8s_cluster.worker_security_group_id
  route53_zone_name        = var.route53_zone_name
  hostnames                = local.ingress_hostnames
}
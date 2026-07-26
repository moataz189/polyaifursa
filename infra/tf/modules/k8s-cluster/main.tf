data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}


# =========================================================
# Security Groups
# =========================================================

resource "aws_security_group" "control_plane" {
  name        = "${var.project_name}-control-plane-sg"
  description = "Security group for the Kubernetes control plane"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH access"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Kubernetes API server from inside the VPC"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  ingress {
    description = "Kubernetes API server public access"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Allow all intra-VPC traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Allow all outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-control-plane-sg"
  }
}


resource "aws_security_group" "workers" {
  name        = "${var.project_name}-workers-sg"
  description = "Security group for Kubernetes worker nodes"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH access"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Allow all intra-VPC traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Allow all outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-workers-sg"
  }
}


# =========================================================
# Control Plane IAM Role
# =========================================================

resource "aws_iam_role" "control_plane" {
  name = "${var.project_name}-control-plane-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Principal = {
          Service = "ec2.amazonaws.com"
        }

        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-control-plane-role"
  }
}


resource "aws_iam_role_policy_attachment" "control_plane_cluster_policy" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}


resource "aws_iam_role_policy_attachment" "control_plane_ebs_policy" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}


resource "aws_iam_role_policy_attachment" "control_plane_ecr_policy" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}


# Allows the control plane to create or update only the join command parameter.
resource "aws_iam_role_policy" "control_plane_ssm" {
  name = "${var.project_name}-control-plane-ssm-policy"
  role = aws_iam_role.control_plane.id

  policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Sid    = "WriteKubernetesJoinCommand"
        Effect = "Allow"

        Action = [
          "ssm:PutParameter",
          "ssm:DeleteParameter"
        ]

        Resource = "arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/moataz-k8s/join-command"
      }
    ]
  })
}


resource "aws_iam_instance_profile" "control_plane" {
  name = "${var.project_name}-control-plane-profile"
  role = aws_iam_role.control_plane.name
}


# =========================================================
# Worker IAM Role
# =========================================================

resource "aws_iam_role" "worker" {
  name = "${var.project_name}-worker-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Principal = {
          Service = "ec2.amazonaws.com"
        }

        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-worker-role"
  }
}


resource "aws_iam_role_policy_attachment" "worker_node_policy" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}


resource "aws_iam_role_policy_attachment" "worker_ebs_policy" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}


resource "aws_iam_role_policy_attachment" "worker_ecr_policy" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}


# Allows workers to read only the Kubernetes join command.
resource "aws_iam_role_policy" "worker_ssm" {
  name = "${var.project_name}-worker-ssm-policy"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Sid    = "ReadKubernetesJoinCommand"
        Effect = "Allow"

        Action = [
          "ssm:GetParameter"
        ]

        Resource = "arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/moataz-k8s/join-command"
      }
    ]
  })
}


resource "aws_iam_instance_profile" "worker" {
  name = "${var.project_name}-worker-profile"
  role = aws_iam_role.worker.name
}


# =========================================================
# Kubernetes Control Plane EC2
# =========================================================

resource "aws_instance" "control_plane" {
  ami                    = var.ami_id
  instance_type          = "t3.medium"
  subnet_id              = var.subnet_ids[0]
  key_name               = var.key_name
  vpc_security_group_ids = [aws_security_group.control_plane.id]

  iam_instance_profile = aws_iam_instance_profile.control_plane.name

  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile(
    "${path.module}/scripts/control-plane.sh",
    {
      region = var.region
    }
  )

  # Recreates the EC2 instance when the user-data script changes.
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tags = {
    Name = "${var.project_name}-control-plane"
    Role = "control-plane"
  }

  depends_on = [
    aws_iam_role_policy.control_plane_ssm
  ]
}
resource "aws_launch_template" "workers" {
  name_prefix   = "${var.project_name}-worker-"
  image_id      = var.ami_id
  instance_type = "t3.medium"
  key_name      = var.key_name

  vpc_security_group_ids = [
    aws_security_group.workers.id
  ]

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  block_device_mappings {
    device_name = "/dev/sda1"

    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  user_data = base64encode(
    templatefile(
      "${path.module}/scripts/worker.sh",
      {
        region = var.region
      }
    )
  )

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tag_specifications {
    resource_type = "instance"

    tags = {
      Name = "${var.project_name}-worker"
      Role = "worker"
    }
  }

  tag_specifications {
    resource_type = "volume"

    tags = {
      Name = "${var.project_name}-worker-volume"
      Role = "worker"
    }
  }

  update_default_version = true

  depends_on = [
    aws_iam_role_policy.worker_ssm
  ]
}
resource "aws_autoscaling_group" "workers" {
  name = "${var.project_name}-workers-asg"

  min_size         = var.worker_min_size
  desired_capacity = var.worker_desired_capacity
  max_size         = var.worker_max_size

  vpc_zone_identifier = var.subnet_ids

  launch_template {
    id      = aws_launch_template.workers.id
    version = "$Latest"
  }

  health_check_type         = "EC2"
  health_check_grace_period = 300

  tag {
    key                 = "Name"
    value               = "${var.project_name}-worker"
    propagate_at_launch = true
  }

  tag {
    key                 = "Role"
    value               = "worker"
    propagate_at_launch = true
  }

  depends_on = [
    aws_instance.control_plane,
    aws_launch_template.workers
  ]
}
data "aws_instances" "workers" {
  filter {
    name   = "tag:aws:autoscaling:groupName"
    values = [aws_autoscaling_group.workers.name]
  }

  depends_on = [aws_autoscaling_group.workers]
}
# Manual node cleanup on scale-down:
# When manually reducing desired_capacity, the terminated instance's Node
# object remains in Kubernetes as NotReady. Run the following after
# confirming the instance has terminated in AWS:
#
#   kubectl delete node <node-name>
#
# (Automatic cleanup via ASG lifecycle hook + Lambda was considered but
# deprioritized in favor of completing the rest of the pipeline; can be
# revisited if time permits.)
terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # 骨架默认本地 state。生产建议改远端 state（S3 + DynamoDB 锁），取消注释并填写：
  # backend "s3" {
  #   bucket         = "your-tfstate-bucket"
  #   key            = "android-package-service/terraform.tfstate"
  #   region         = "us-west-2"
  #   dynamodb_table = "your-tf-lock-table"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region
}

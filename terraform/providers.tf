locals {
  default_tags = {
    Project     = "aws-inventory-metadata-discovery"
    Environment = var.env
    ManagedBy   = "terraform"
  }
}

# One provider for every region: regional resources outside the primary region set
# `region` explicitly (AWS provider 6.x).
provider "aws" {
  region = var.primary_region
  default_tags { tags = local.default_tags }
}

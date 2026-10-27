locals {
  default_tags = {
    Project     = "aws-inventory-metadata-discovery"
    Environment = var.env
    ManagedBy   = "terraform"
  }
}

# The home region hosts the control plane (database, broker, planner, API).
provider "aws" {
  region = var.home_region
  default_tags { tags = local.default_tags }
}

provider "aws" {
  alias  = "ap_east_1"
  region = "ap-east-1"
  default_tags { tags = local.default_tags }
}

provider "aws" {
  alias  = "eu_west_1"
  region = "eu-west-1"
  default_tags { tags = local.default_tags }
}

provider "aws" {
  alias  = "eu_west_2"
  region = "eu-west-2"
  default_tags { tags = local.default_tags }
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags { tags = local.default_tags }
}

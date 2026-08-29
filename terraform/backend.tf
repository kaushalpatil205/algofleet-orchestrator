terraform {
  backend "s3" {
    bucket       = "algofleet-tf-state-kaushal-2026"
    key          = "prod/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}

locals {
  config = yamldecode(file("${path.module}/config.yaml"))
}

module "compute" {
  source    = "../../modules/compute"
  instances = local.config.instances
}

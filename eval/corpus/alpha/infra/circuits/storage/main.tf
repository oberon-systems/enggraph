locals {
  config = yamldecode(file("${path.module}/config.yaml"))
}

resource "cloudflare_r2_bucket" "bucket" {
  for_each = local.config.buckets
  name     = each.key
}

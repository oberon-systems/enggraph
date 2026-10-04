variable "instances" {
  type = map(any)
}

resource "openstack_compute_instance_v2" "host" {
  for_each    = var.instances
  name        = each.key
  flavor_name = each.value.flavor
  image_name  = each.value.image
}

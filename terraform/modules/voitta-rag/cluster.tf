# GKE Standard, single-zone, single node pool.
#
# Why Standard rather than Autopilot: the workload is a single stateful
# replica with a hard CPU-family preference (C4 / Sapphire-Rapids for AMX).
# Autopilot's compute classes can target C-family but at a per-pod premium
# and with less deterministic scheduling — Standard is simpler and cheaper
# for a fixed single-pod workload.

resource "google_container_cluster" "this" {
  name     = "${local.prefix}-cluster"
  project  = var.project_id
  location = var.zone

  # Cluster + default pool can't be created together cleanly when we want
  # custom machine types, so we make + drop the default and add our own.
  remove_default_node_pool = true
  initial_node_count       = 1

  # Default network is fine for a single-replica deploy. Customers who
  # need shared VPC/PSC can switch this without touching workloads.
  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {}

  # Kubelet logs go to Cloud Logging; metrics to Cloud Monitoring. Keeps
  # the on-call story sane without paying for extra agents.
  logging_service    = "logging.googleapis.com/kubernetes"
  monitoring_service = "monitoring.googleapis.com/kubernetes"

  # Disable the legacy basic auth + the unauthenticated cert. Required for
  # any modern GKE deployment.
  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  # Allow ``terraform destroy`` to take the cluster offline. Production
  # deployments should set deletion_protection = true via override.
  deletion_protection = false

  depends_on = [google_project_service.apis]
}

resource "google_container_node_pool" "primary" {
  name       = "${local.prefix}-pool"
  project    = var.project_id
  location   = var.zone
  cluster    = google_container_cluster.this.name
  node_count = 1

  node_config {
    machine_type = var.machine_type
    disk_size_gb = 50
    disk_type    = "pd-balanced"

    # OAuth scope ``cloud-platform`` is the modern catch-all; finer-grained
    # IAM is enforced at the resource level. Without it, even basic ops
    # like reading from the pinned image registry can fail.
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 0
    max_unavailable = 1
  }
}

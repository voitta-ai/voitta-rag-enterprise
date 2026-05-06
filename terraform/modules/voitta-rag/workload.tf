# Kubernetes-side resources: namespace, PVC, ConfigMap (users.txt), Secret
# (OAuth + session), Deployment, Service, Ingress.
#
# The kubernetes provider's auth comes from the cluster data sources in
# providers.tf. Everything below is plain k8s — no Helm, no kustomize.

locals {
  k8s_labels = {
    "app.kubernetes.io/name"       = local.prefix
    "app.kubernetes.io/managed-by" = "terraform"
  }

  # Built-in env that the module always sets. Caller-supplied extras
  # merge over the top so an operator can override anything in extra_env.
  base_env = merge(
    {
      VOITTA_DATA_DIR        = "/data"
      VOITTA_ROOT_PATH       = "/data/folders"
      VOITTA_USERS_FILE      = "/etc/voitta/users.txt"
      VOITTA_PORT            = "8000"
      VOITTA_ALLOWED_DOMAINS = join(",", var.allowed_domains)
    },
    var.extra_env,
  )
}

resource "kubernetes_namespace" "this" {
  metadata {
    name   = local.prefix
    labels = local.k8s_labels
  }

  depends_on = [google_container_node_pool.primary]
}

resource "kubernetes_persistent_volume_claim" "data" {
  metadata {
    name      = "${local.prefix}-data"
    namespace = kubernetes_namespace.this.metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = {
        storage = "${var.data_disk_gb}Gi"
      }
    }
    storage_class_name = (
      var.data_disk_type == "pd-ssd" ? "premium-rwo"
      : var.data_disk_type == "pd-balanced" ? "standard-rwo"
      : "standard-rwo"
    )
  }

  # Wait for the actual pod to bind. Without this, an apply sometimes
  # finishes before GKE has provisioned the underlying disk.
  wait_until_bound = false
}

resource "kubernetes_config_map" "users" {
  metadata {
    name      = "${local.prefix}-users"
    namespace = kubernetes_namespace.this.metadata[0].name
    labels    = local.k8s_labels
  }

  data = {
    "users.txt" = join("\n", concat(
      ["# Managed by Terraform — extra_users variable. Do not edit by hand."],
      var.extra_users,
      [""],
    ))
  }
}

resource "kubernetes_secret" "auth" {
  metadata {
    name      = "${local.prefix}-auth"
    namespace = kubernetes_namespace.this.metadata[0].name
    labels    = local.k8s_labels
  }

  type = "Opaque"
  data = {
    google_oauth_client_id     = var.google_oauth_client_id
    google_oauth_client_secret = var.google_oauth_client_secret
    session_secret             = var.session_secret
  }
}

resource "kubernetes_deployment" "app" {
  metadata {
    name      = local.prefix
    namespace = kubernetes_namespace.this.metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    replicas = 1

    # Recreate, never RollingUpdate. SQLite is single-writer and embedded
    # Qdrant holds an exclusive lock on its data dir — two pods cannot
    # share the PD safely. Brief downtime on update is the trade-off.
    strategy {
      type = "Recreate"
    }

    selector {
      match_labels = { "app.kubernetes.io/name" = local.prefix }
    }

    template {
      metadata {
        labels = local.k8s_labels
        annotations = {
          # Roll the pod whenever users.txt or auth secret changes so the
          # new value is actually picked up. (Subpath mounts of ConfigMaps
          # do not update in place.)
          "checksum/users-config" = sha256(jsonencode(kubernetes_config_map.users.data))
          "checksum/auth-secret"  = sha256(jsonencode(kubernetes_secret.auth.data))
        }
      }

      spec {
        # Don't fight every other pod for the CPU. The node has 8 vCPU
        # and the app expects most of them under indexing. Setting both
        # makes kube-scheduler park us cleanly.
        container {
          name  = "app"
          image = var.image_uri

          port {
            name           = "http"
            container_port = 8000
          }

          env {
            name = "VOITTA_GOOGLE_AUTH_CLIENT_ID"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.auth.metadata[0].name
                key  = "google_oauth_client_id"
              }
            }
          }

          env {
            name = "VOITTA_GOOGLE_AUTH_CLIENT_SECRET"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.auth.metadata[0].name
                key  = "google_oauth_client_secret"
              }
            }
          }

          env {
            name = "VOITTA_SESSION_SECRET"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.auth.metadata[0].name
                key  = "session_secret"
              }
            }
          }

          dynamic "env" {
            for_each = local.base_env
            content {
              name  = env.key
              value = env.value
            }
          }

          volume_mount {
            name       = "data"
            mount_path = "/data"
          }

          volume_mount {
            name       = "users"
            mount_path = "/etc/voitta"
            read_only  = true
          }

          # /api/health is light and dependency-free. We use it for both
          # liveness and readiness — startup probe handles slow boots.
          liveness_probe {
            http_get {
              path = "/healthz"
              port = "http"
            }
            period_seconds        = 30
            timeout_seconds       = 5
            failure_threshold     = 3
            initial_delay_seconds = 0
          }

          readiness_probe {
            http_get {
              path = "/healthz"
              port = "http"
            }
            period_seconds    = 10
            timeout_seconds   = 5
            failure_threshold = 3
          }

          # First boot reads embedded models off the image, opens Qdrant
          # storage, and binds. ~30s normally, allow up to 5 min.
          startup_probe {
            http_get {
              path = "/healthz"
              port = "http"
            }
            period_seconds    = 5
            failure_threshold = 60
          }

          resources {
            requests = {
              cpu    = "1500m"
              memory = "4Gi"
            }
            limits = {
              memory = "24Gi"
            }
          }
        }

        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.data.metadata[0].name
          }
        }

        volume {
          name = "users"
          config_map {
            name = kubernetes_config_map.users.metadata[0].name
          }
        }

        # Single-zone, fixed-shape pod — no anti-affinity needed.
        termination_grace_period_seconds = 30
      }
    }
  }
}

resource "kubernetes_service" "app" {
  metadata {
    name      = local.prefix
    namespace = kubernetes_namespace.this.metadata[0].name
    labels    = local.k8s_labels

    annotations = {
      # GCE Ingress backend — sets up NEGs against the pods directly,
      # which is required for the global LB to work.
      "cloud.google.com/neg" = jsonencode({ ingress = true })
    }
  }

  spec {
    selector = { "app.kubernetes.io/name" = local.prefix }

    port {
      name        = "http"
      port        = 80
      target_port = "http"
    }

    # NodePort, not LoadBalancer — the Ingress fronts everything.
    type = "NodePort"
  }
}

resource "kubernetes_ingress_v1" "app" {
  metadata {
    name      = local.prefix
    namespace = kubernetes_namespace.this.metadata[0].name
    labels    = local.k8s_labels

    annotations = {
      "kubernetes.io/ingress.class"                 = "gce"
      "kubernetes.io/ingress.global-static-ip-name" = google_compute_global_address.ingress.name
      # No managed cert here — the operator wires DNS + ManagedCertificate
      # in a manual follow-up step (documented in terraform/README.md).
      # That keeps the module deterministic; the cert can be provisioned
      # before or after the first apply.
    }
  }

  spec {
    default_backend {
      service {
        name = kubernetes_service.app.metadata[0].name
        port {
          number = 80
        }
      }
    }
  }
}

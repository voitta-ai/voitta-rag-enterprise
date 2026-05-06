# Optional global HTTPS load balancer. All resources gated on
# var.create_load_balancer; when false, every count = 0 and the
# default Caddy-on-VM path (#5) handles TLS.
#
# Topology:
#
#   client → global forwarding rule (443) → target HTTPS proxy
#          → URL map → backend service → instance group (the VM)
#                                 |
#                                 └── health check on /healthz:8000
#
# Plus a port-80 forwarding rule that returns a 301 to https.

locals {
  lb_count = var.create_load_balancer ? 1 : 0

  # Cert covers the primary domain + any aliases.
  lb_cert_domains = concat([var.domain], var.domain_aliases)
}

# Global static IP for the LB. Customer's DNS A record points here.
resource "google_compute_global_address" "lb" {
  count = local.lb_count

  name         = "${local.prefix}-lb-ip"
  address_type = "EXTERNAL"
  ip_version   = "IPV4"
}

# Health check: GCP LB hits the VM's HTTP port directly. The app's
# /healthz returns {"ok": true} as soon as the lifespan is up.
resource "google_compute_health_check" "app" {
  count = local.lb_count

  name = "${local.prefix}-app-hc"

  timeout_sec         = 5
  check_interval_sec  = 10
  healthy_threshold   = 2
  unhealthy_threshold = 3

  http_health_check {
    port         = 8000
    request_path = "/healthz"
  }

  log_config {
    enable = true
  }
}

# Unmanaged instance group of one. The named port "http" → 8000 is
# what the backend service references when wiring traffic.
resource "google_compute_instance_group" "app" {
  count = local.lb_count

  name      = "${local.prefix}-ig"
  zone      = var.zone
  instances = [google_compute_instance.vm.self_link]

  named_port {
    name = "http"
    port = 8000
  }
}

# Backend service. timeout_sec at 3600 lets WebSocket connections
# survive the live event stream's natural lulls; the SPA reconnects
# on its own beyond that.
resource "google_compute_backend_service" "app" {
  count = local.lb_count

  name                  = "${local.prefix}-backend"
  protocol              = "HTTP"
  port_name             = "http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 3600

  health_checks = [google_compute_health_check.app[0].id]

  backend {
    group           = google_compute_instance_group.app[0].self_link
    balancing_mode  = "UTILIZATION"
    capacity_scaler = 1.0
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

# Google-managed SSL cert. Provisioning takes 15-60 min after DNS
# resolves; that wait happens AFTER the customer's first apply, not
# during it.
resource "google_compute_managed_ssl_certificate" "app" {
  count = local.lb_count

  name = "${local.prefix}-cert"

  managed {
    domains = local.lb_cert_domains
  }
}

# URL map for HTTPS — everything to the single backend.
resource "google_compute_url_map" "https" {
  count = local.lb_count

  name            = "${local.prefix}-https-urlmap"
  default_service = google_compute_backend_service.app[0].id
}

resource "google_compute_target_https_proxy" "https" {
  count = local.lb_count

  name             = "${local.prefix}-https-proxy"
  url_map          = google_compute_url_map.https[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.app[0].id]
}

resource "google_compute_global_forwarding_rule" "https" {
  count = local.lb_count

  name                  = "${local.prefix}-https-fr"
  ip_address            = google_compute_global_address.lb[0].address
  port_range            = "443"
  target                = google_compute_target_https_proxy.https[0].id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# Port-80 → 301 https://<host><path>. Pure URL-map redirect; no
# backend traffic involved.
resource "google_compute_url_map" "http_redirect" {
  count = local.lb_count

  name = "${local.prefix}-http-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "http" {
  count = local.lb_count

  name    = "${local.prefix}-http-proxy"
  url_map = google_compute_url_map.http_redirect[0].id
}

resource "google_compute_global_forwarding_rule" "http" {
  count = local.lb_count

  name                  = "${local.prefix}-http-fr"
  ip_address            = google_compute_global_address.lb[0].address
  port_range            = "80"
  target                = google_compute_target_http_proxy.http[0].id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# Allow GCP LB health-check + serving ranges to reach port 8000 on the
# VM. These two CIDRs are GCP-published and stable; documented at
# https://cloud.google.com/load-balancing/docs/health-check-concepts#ip-ranges
resource "google_compute_firewall" "allow_lb_health" {
  count = local.lb_count

  name    = "${local.prefix}-allow-lb-health"
  network = google_compute_network.vpc.id

  direction     = "INGRESS"
  source_ranges = ["35.191.0.0/16", "130.211.0.0/22"]
  target_tags   = ["${local.prefix}-lb"]

  allow {
    protocol = "tcp"
    ports    = ["8000"]
  }
}

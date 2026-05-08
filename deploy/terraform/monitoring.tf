# Optional baseline monitoring. All resources gated on
# var.enable_monitoring; when false no Cloud Monitoring resources
# are created.
#
# What ships:
#   - Email notification channel pointed at var.alert_email.
#   - Uptime check on https://${var.domain}/healthz, every 60s.
#     (Skipped when var.domain is blank since there's no public host
#     to probe.)
#   - Alert policy: uptime check failed for 2 consecutive minutes.
#   - Log-based metric counting ERROR-or-worse log lines from the VM.
#   - Alert policy: error metric > 10/min for 5 min.

locals {
  monitoring_count = var.enable_monitoring ? 1 : 0
  uptime_count     = var.enable_monitoring && var.domain != "" ? 1 : 0
}

resource "google_monitoring_notification_channel" "email" {
  count = local.monitoring_count

  display_name = "${local.prefix}-email"
  type         = "email"

  labels = {
    email_address = var.alert_email
  }
}

resource "google_monitoring_uptime_check_config" "healthz" {
  count = local.uptime_count

  display_name = "${local.prefix}-healthz"
  timeout      = "10s"
  period       = "60s"

  http_check {
    path           = "/healthz"
    port           = "443"
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.domain
    }
  }
}

resource "google_monitoring_alert_policy" "uptime" {
  count = local.uptime_count

  display_name = "${local.prefix}-uptime"
  combiner     = "OR"

  conditions {
    display_name = "uptime check failed"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\"",
        "resource.type=\"uptime_url\"",
        "metric.labels.check_id=\"${google_monitoring_uptime_check_config.healthz[0].uptime_check_id}\"",
      ])
      duration        = "120s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].name]
}

# Log-based metric counting ERROR+ log entries from this project's VM.
resource "google_logging_metric" "voitta_errors" {
  count = local.monitoring_count

  name   = "${local.prefix}-app-errors"
  filter = "resource.type=\"gce_instance\" AND severity>=ERROR"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "errors" {
  count = local.monitoring_count

  display_name = "${local.prefix}-error-rate"
  combiner     = "OR"

  conditions {
    display_name = "error rate >10/min for 5 min"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.voitta_errors[0].name}\"",
        "resource.type=\"gce_instance\"",
      ])
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 10
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].name]
}

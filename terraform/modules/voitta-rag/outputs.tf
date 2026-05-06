output "ingress_ip" {
  description = "Reserved global IPv4 the LB answers on. Point a DNS A record at this to wire up the customer's hostname."
  value       = google_compute_global_address.ingress.address
}

output "redirect_uri" {
  description = "Authorized redirect URI to register on the OAuth client. Replace <host> with whatever DNS name resolves to ingress_ip."
  value       = "https://<host>/api/auth/google/callback"
}

output "cluster_name" {
  description = "GKE cluster name — feed into ``gcloud container clusters get-credentials`` to talk kubectl directly."
  value       = google_container_cluster.this.name
}

output "namespace" {
  description = "Kubernetes namespace the workload runs in."
  value       = kubernetes_namespace.this.metadata[0].name
}

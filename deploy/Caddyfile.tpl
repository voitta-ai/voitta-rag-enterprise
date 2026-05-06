# voitta-rag Caddyfile.
#
# Rendered by terraform / cloud-init at first boot — ${DOMAIN} is
# substituted in place. Caddy auto-issues a Let's Encrypt cert for the
# domain and auto-redirects port 80 → 443; we only need to spell out
# the application proxy and the headers we care about.
#
# Customer-facing prereq: an A record for ${DOMAIN} must already point
# at the VM's static external IP (terraform output `vm_external_ip`)
# before this file is rendered, or the ACME HTTP-01 challenge fails
# and Caddy serves with a self-signed cert until DNS catches up.

${DOMAIN} {
	encode gzip

	# Force HTTPS in clients for a year, including subdomains. preload
	# is intentionally NOT set — only flip it on once the customer has
	# confirmed they are committed to HTTPS-only forever.
	header Strict-Transport-Security "max-age=31536000; includeSubDomains"

	# WebSocket upgrades (the live event stream) and any future SSE
	# routes flow through the same reverse_proxy. flush_interval -1
	# disables Go's http.Transport response buffering so partial
	# responses ship out immediately — important for the indexing job
	# feed.
	reverse_proxy 127.0.0.1:8000 {
		flush_interval -1
	}
}

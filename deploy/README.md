# `deploy/` — Customer-installable GCP infrastructure

This folder contains everything needed to stand up `voitta-rag-enterprise`
on a customer's own GCP project. The primary install path is the Tier A
single-VM deploy.

## Layout

```
deploy/
├── Dockerfile             runtime image (CPU base, single-stage)
├── docker-entrypoint.sh   resolves sm:// env-var refs at boot
├── Caddyfile.tpl          TLS reverse proxy (default path)
├── cloud-init.yaml.tftpl  first-boot bootstrap
├── systemd/
│   ├── voitta.service     application unit
│   └── caddy.service      TLS proxy unit
├── terraform/             VPC + VM + disk + LB + KMS + ...
└── scripts/               operator helpers (init, logs, shell, ...)
```

## Prerequisites

`gcloud` (logged in via `gcloud auth application-default login`),
`terraform >= 1.6`, `bash`, `make`. Operator's GCP identity needs
Owner on the target project for first-time setup.

## First deploy

See [`docs/DEPLOY.md`](../docs/DEPLOY.md). Short version:

```bash
# Run from the repo root.
cp deploy/terraform/.env.terraform.example .env.terraform   # fill in
cp .env.secrets.example                    .env.secrets     # fill in
make deploy-init
make deploy-plan
make deploy-apply
make deploy-secrets
```

## Common ops

| Task | Command |
|---|---|
| Tail logs | `make deploy-logs` |
| SSH in | `make deploy-shell` |
| Roll forward image | `make deploy-upgrade` |
| Ad-hoc snapshot | `make deploy-backup-now` |
| Tear it down | `make deploy-destroy` |

## See also

- [`../docs/DEPLOY.md`](../docs/DEPLOY.md) — full install walkthrough,
  costs, troubleshooting, rotation, restore.
- [`../README.md`](../README.md) — application overview.
- [`./terraform/.env.terraform.example`](./terraform/.env.terraform.example)
  and [`../.env.secrets.example`](../.env.secrets.example) — config templates.

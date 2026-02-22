# Notes App (FastAPI + PostgreSQL + GitOps)

## What it is
A container-ready notes service combining a FastAPI backend, simple frontend, and Kubernetes deployment assets.

## What it does
- Exposes CRUD APIs for notes with health and readiness endpoints.
- Supports environment-driven database configuration (PostgreSQL/SQLite fallback rules).
- Ships with GitOps-friendly manifests and test coverage for deployment confidence.

## Why it matters
It demonstrates end-to-end service engineering: API behavior, persistence control, observability hooks, and reproducible cluster deployment.

This repository contains a production-ish demo notes service with:

- FastAPI backend and static frontend
- PostgreSQL for persistence
- Kubernetes manifests managed by Kustomize
- ArgoCD-compatible overlay at `deploy/overlays/dev`
- Optional Prometheus scraping via ServiceMonitor
- CI for tests and manifest rendering

## API endpoints

Stable endpoints:

- `GET /` - serves UI
- `GET /healthz` - liveness
- `GET /readyz` - readiness (checks DB connectivity)
- `GET /metrics` - Prometheus metrics
- `GET /api/notes`
- `POST /api/notes`
- `PUT /api/notes/{id}`
- `DELETE /api/notes/{id}` (archives note)
- `DELETE /api/notes/{id}/purge` (permanent delete)
- `POST /api/notes/{id}/archive`
- `POST /api/notes/{id}/unarchive`
- `POST /api/notes/{id}/share`
- `GET /api/notes/{id}/history`
- `GET /api/notes/{id}/attachments`
- `POST /api/notes/{id}/attachments` (multipart file upload)
- `GET /api/attachments/{id}/download`
- `GET /api/attachments/{id}/preview` (PPTX slide-text preview)
- `DELETE /api/attachments/{id}`
- `GET /api/share/{token}` (JSON read-only view)
- `GET /share/{token}` (read-only HTML page)

## Local development

### 1) Run PostgreSQL with Docker

```bash
docker run --rm -d \
  --name notes-postgres \
  -e POSTGRES_DB=notes \
  -e POSTGRES_USER=notes \
  -e POSTGRES_PASSWORD=notes-dev-password \
  -p 5432:5432 \
  postgres:16-alpine
```

### 2) Run app

```bash
cd app
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export POSTGRES_HOST=127.0.0.1
export POSTGRES_PORT=5432
export POSTGRES_DB=notes
export POSTGRES_USER=notes
export POSTGRES_PASSWORD=notes-dev-password
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 3) Quick smoke checks

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/api/notes
```

Database backend selection rules:

- Postgres is selected when `DB_HOST` or `DATABASE_URL` is set.
- SQLite is selected only when `DB_PATH` is set.
- Startup fails with a clear error when none of these are set.

Attachment upload defaults:

- `UPLOAD_DIR` controls attachment storage path (default `/data/uploads`).
- `UPLOAD_MAX_SIZE_MB` controls max upload size (default `700`).


## Testing

```bash
cd app
pytest -q
```

## GitOps deployment (ArgoCD)

ArgoCD source path stays:

- `deploy/overlays/dev`

Render locally:

```bash
kustomize build deploy/overlays/dev
```

Apply manually:

```bash
kubectl apply -k deploy/overlays/dev
```

## Ingress and URLs

Dev overlay host is patched in `deploy/overlays/dev/ingress-host.yaml`.

Pattern:

- `notes.<NODE_IP>.nip.io`
- `docs.<NODE_IP>.nip.io` (OnlyOffice visual PPT preview)

Update that file to match your cluster node IP before deploy.

## Metrics in Prometheus/Grafana

The app always exposes `/metrics`.

A `ServiceMonitor` is provided as an optional Kustomize component:

- `deploy/components/monitoring`

Enable it in `deploy/overlays/dev/kustomization.yaml` by uncommenting:

```yaml
components:
  - ../../components/monitoring
```

Use this only when `monitoring.coreos.com` CRDs (from kube-prometheus-stack) are available.

In Grafana, query app metrics such as request rate/latency from the scraped `notes-app` target.

## Security defaults in Kubernetes

The deployment enables:

- `runAsNonRoot: true`
- `runAsUser: 65534`
- `fsGroup: 65534`
- `seccompProfile: RuntimeDefault`
- `readOnlyRootFilesystem: true`
- `allowPrivilegeEscalation: false`
- capabilities drop `ALL`
- `automountServiceAccountToken: false`
- requests/limits for app and postgres

A NetworkPolicy allows ingress only from an ingress-controller namespace label. In dev overlay, it is set to namespace `ingress` for MicroK8s and can be patched to `ingress-nginx` or another namespace as needed.

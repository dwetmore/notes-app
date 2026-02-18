# Notes App (FastAPI + PostgreSQL + GitOps)

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
- `DELETE /api/notes/{id}`

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

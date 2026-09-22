# Callspire PBX Gateway

**Canonical GitHub repo:** [Intteger157/Callspire-PBX-Gateway](https://github.com/Intteger157/Callspire-PBX-Gateway)

FastAPI service that runs **next to MikoPBX** on Linux: CDR lookup, AMI originate, admin panel, WebRTC settings, Kommo/AmoCRM integration, and (optionally) the browser web softphone at `/softphone/`.

Typical install path on a server: `/opt/callspire/pbx-gateway` (see the [stack installer](https://github.com/Intteger157/Callspire.Gateway-for-MikoPBX)). Legacy path: `/opt/callspire-pbx-gateway` or `/opt/mikopbx-cdr-proxy`.

When developing inside the monorepo `Softphone-crossplatform`, this directory is a **gitignored sibling clone** — edit here, push to **Callspire-PBX-Gateway**, not deploy mirrors under `deploy/`.

## Repository layout

```
callspire-pbx-gateway/
├── README.md
├── app.py, app_kommo.py         ← FastAPI gateway + Kommo API
├── cdr_client.py, config.py     ← Miko CDR / config
├── kommo_crm.py, kommo_recording.py, kommo_call_worker.py
├── kommo_jobs_db.py, kommo_store.py, kommo_oauth.py, kommo_service.py
├── permissions_db.py
├── config.example.yaml          ← copy to config.yaml (never commit config.yaml)
├── .env.example                 ← optional env for SESSION_SECRET, SOFTPHONE_STATIC_DIR, …
├── DEPLOY_CHECKLIST.ru.md
├── scripts/deploy-to-prod.sh
├── templates/                   ← admin UI (+ Kommo)
├── gateway-web-softphone/       ← Python mount package (same-origin /softphone/)
└── requirements.txt
```

The **Vue SPA sources** live in **[Callspire-web-softphone](https://github.com/Intteger157/Callspire-web-softphone)**. Build `softphone-web/dist/` and point `SOFTPHONE_STATIC_DIR` at it (or use the prebuilt `dist/` bundled in [Callspire.Gateway-for-MikoPBX](https://github.com/Intteger157/Callspire.Gateway-for-MikoPBX)).

## Configuration (no secrets in git)

| File | Purpose |
|------|---------|
| **`config.example.yaml`** | Gateway template: JWT, MikoPBX paths, Kommo workers, default admin (`admin` / `admin`). Copy to `config.yaml`. |
| **`.env.example`** | Optional overrides: `SESSION_SECRET`, `SOFTPHONE_STATIC_DIR`, WebRTC fallbacks. Copy to `.env`. |

**Do not commit:** `config.yaml`, `.env`, `*.sqlite`, `permissions.db` — runtime data only.

## Quick start (dev)

### 1) Build web UI (optional)

```bash
git clone https://github.com/Intteger157/Callspire-web-softphone.git
cd callspire-web-softphone/softphone-web
npm ci && npm run build
# → dist/
```

### 2) Python venv + gateway

```bash
cd callspire-pbx-gateway
cp config.example.yaml config.yaml   # edit paths / jwt_secret
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e ./gateway-web-softphone
export SESSION_SECRET="$(openssl rand -hex 32)"
export SOFTPHONE_STATIC_DIR="/path/to/callspire-web-softphone/softphone-web/dist"
uvicorn app:app --host 0.0.0.0 --port 8443
```

Open **`http://127.0.0.1:8443/admin`** (default **admin / admin** — change on first login) and **`http://127.0.0.1:8443/softphone/`** when static dir is set.

### 3) Mount web softphone in `app.py`

At the **end** of `app.py` (after all routers):

```python
from gateway_web_softphone import install_web_softphone

install_web_softphone(app)
```

Details: `gateway-web-softphone/README.md`, `INTEGRATION_APP_EXAMPLE.py`.

Production uses **one uvicorn process** (gateway + `/softphone/` + `/api/*`). The legacy Node **softphone-bff** is removed — do not deploy it for new installs.

## Kommo CRM integration

Admin UI: **Settings → Kommo CRM**. One OAuth authorization for all Callspire clients (desktop + web).

**Redirect URI** in Kommo (must match exactly):

```
https://<your-gateway-host>/<base-path>/oauth/kommo/callback
```

**Per-user exclusions:** PBX Users → **Gateway Kommo** → uncheck **Use shared** so an extension skips the company Kommo session (desktop can still use local Kommo).

Kommo upload pipeline:

| File | Role |
|------|------|
| `kommo_recording.py` | CDR match, disposition, recording fetch |
| `kommo_call_worker.py` | Background job worker |
| `kommo_crm.py` | Notes / attachments in Kommo |
| `kommo_jobs_db.py` | Job queue + admin log |

See **`DEPLOY_CHECKLIST.ru.md`**, **`README_KOMMO.md`**.

## Deploy to existing server

```powershell
.\copy-to-server.ps1 -Server root@your-host -GatewayPath /opt/callspire/pbx-gateway
```

```bash
./scripts/deploy-to-prod.sh root@your-host /opt/callspire/pbx-gateway
```

Or use the full stack installer: **[Callspire.Gateway-for-MikoPBX](https://github.com/Intteger157/Callspire.Gateway-for-MikoPBX)**.

## Related repositories

| Repository | Role |
|---|---|
| **[Callspire.Gateway-for-MikoPBX](https://github.com/Intteger157/Callspire.Gateway-for-MikoPBX)** | One-command Linux installer (bundles gateway + prebuilt web `dist/`) |
| **[Callspire-web-softphone](https://github.com/Intteger157/Callspire-web-softphone)** | Vue SPA sources |
| **[Callspire-softphone](https://github.com/Intteger157/Callspire-softphone)** | Windows + macOS desktop clients |

---

Do **not** treat `deploy/web-softphone/gateway-patch/` in the monorepo as source of truth — edit **this repo** only.

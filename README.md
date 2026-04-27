# Callspire PBX Gateway

**Previously shipped as “MikoPBX CDR Proxy”.** Service that runs on or next to MikoPBX and exposes a **JWT-authenticated HTTP API** for Callspire (Windows softphone, web softphone BFF, integrations): **CDR**, **recordings**, **outbound Caller ID**, **originate**, WebRTC/SIP settings, admin tooling, and more.

## Why

MikoPBX CDR API (`/pbxcore/api/cdr/get_data`) is effectively localhost-oriented; exposing it remotely is awkward. This gateway adds JWT authentication so Callspire clients can use CDR safely — historically the main driver was resolving **outbound CallerID** from trunk configuration (also available via REST on recent builds).

## Authentication model (recommended for internet-facing deployments)

There are two user types:

- **Admin**: credentials stored in `config.yaml` (bcrypt). Admins can manage CallerID permissions, trunk numbers and app users.
- **App users (recommended)**: users sign in with **corporate email + password**, and each app user is mapped to a **MikoPBX extension**. This avoids exposing MikoPBX SIP passwords (`m_Sip.secret`) to the internet.

On first login, an app user is required to change their password (`must_change_password` flag).

## Quick Start

### 1. Install

```bash
# On the MikoPBX server
cd /opt
git clone <repo-url> callspire-pbx-gateway
cd callspire-pbx-gateway
pip install -r requirements.txt
```

### 2. Configure

Edit `config.yaml`:

```yaml
host: "0.0.0.0"
port: 8443
jwt_secret: "generate-a-random-string-here"
jwt_expire_days: 30
mikopbx_url: "http://127.0.0.1"

# Optional (recommended if proxy is exposed to the internet):
# require this header on login endpoints (/auth/login, /api/v1/auth/login)
# so only your BFF can perform password authentication.
service_token: "generate-a-long-random-string"

# For HTTPS (recommended):
ssl_certfile: "/etc/letsencrypt/live/yourdomain/fullchain.pem"
ssl_keyfile: "/etc/letsencrypt/live/yourdomain/privkey.pem"
```

### 3. Create a user

Generate a bcrypt password hash:

```bash
python3 -c "import bcrypt; print(bcrypt.hashpw(b'huvuTTYUVUkjhUGyfTjgH', bcrypt.gensalt()).decode())"
```

Add it to `config.yaml`:

```yaml
users:
  - username: "admin"
    password_hash: "$2b$12$..."
```

If no users are configured, the service auto-generates one on first start and prints the password to stdout.

### 4. Run

```bash
python app.py
```

Or with systemd (see below).

### 5. Test

```bash
# Get a token
TOKEN=$(curl -s -X POST http://localhost:8443/auth/login \
  -d "username=admin&password=your-password" | python -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Query CDR
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8443/api/cdr?ext=204&limit=5"
```

## SSL with Let's Encrypt

```bash
apt install certbot
certbot certonly --standalone -d pbx.yourdomain.com
```

Then set `ssl_certfile` and `ssl_keyfile` in `config.yaml`.

## Self-signed certificate

```bash
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes \
  -subj "/CN=pbx.yourdomain.com"
```

## Systemd service

Create `/etc/systemd/system/callspire-pbx-gateway.service`:

```ini
[Unit]
Description=Callspire PBX Gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/callspire-pbx-gateway
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

*(If you still use an old unit named `cdr-proxy`, either rename it or keep the old paths — only one working directory matters.)*

```bash
systemctl daemon-reload
systemctl enable --now callspire-pbx-gateway
```

## MikoPBX REST API (v3) mode

On MikoPBX builds that ship the v3 REST API (`/pbxcore/api/v3/...`), the proxy can talk to MikoPBX over HTTP instead of opening the SQLite files directly. Benefits:

- no host-filesystem access to `mikopbx_storage` / `mikopbx_cf` volumes required,
- recordings are streamed through short-lived signed URLs,
- provider health + active calls become available (new admin endpoints),
- works cleanly when MikoPBX is fronted by nginx and the proxy lives on a different host.

### Enable it

1. In MikoPBX UI: **Settings → API Keys → New key** (scopes: `cdr:read`, `extensions:read`, `sip-providers:read`, plus `pbx-status:read` if you want active-calls).
2. Paste the key into `config.yaml`:
   ```yaml
   use_rest_api: true
   mikopbx_rest_url: "http://127.0.0.1"   # or https://pbx.example.com
   mikopbx_api_key: "eyJhbGciOi..."
   mikopbx_verify_ssl: true                # set false for self-signed LAN certs
   ```
3. Restart `callspire-pbx-gateway` and check the startup log — you should see:
   ```
   [pbx-gateway] REST URL:  http://127.0.0.1
   [pbx-gateway] REST ping: OK
   ```
4. Sanity check from the proxy host:
   ```bash
   TOKEN=$(curl -s -X POST http://localhost:8005/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"..."}' | jq -r .token)
   # CDR now comes from MikoPBX REST, not from SQLite
   curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8005/api/cdr?limit=3" | jq
   # Provider statuses (REST-only, 501 when use_rest_api=false)
   curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8005/api/admin/provider-statuses" | jq
   ```

If you don't want a long-lived API key, use admin credentials instead — the client will log in via `POST /auth:login` and refresh the JWT automatically:

```yaml
use_rest_api: true
mikopbx_rest_url: "http://127.0.0.1"
mikopbx_admin_login: "admin"
mikopbx_admin_password: "..."
```

### What stays on SQLite

Even with REST enabled:

- `authenticate_mikopbx_user` (extension + SIP password → JWT) still reads `m_Sip.secret` from the config DB, because the REST API does not expose SIP secrets for obvious reasons. Set `use_rest_api: false` back if you don't want the proxy to touch the config DB at all — then log in with email/app-user credentials instead of SIP extension.
- `GET /api/v1/webrtc/config` resolves `sipPassword` by trying **`<ext>-WS`** and **`<ext>`** (REST, then pjsip; WebRTC is often under the `-WS` auth), and may return `sipAuthUser` so the browser uses the same digest user as PJSIP.

### Rollback

The REST toggle is a soft switch: flip `use_rest_api: false` and restart; everything falls back to the legacy SQLite + `docker cp` path with zero other changes.

## Web admin (`/admin`)

The Callspire admin UI is a single-page shell under **`/admin`**. Templates use **`base_path`** from the **`X-Forwarded-Prefix`** header (same idea as reverse-proxy deployments): favicon, `/static/*`, and JavaScript API calls go through **`resolvePublicUrl()`** — no hardcoded absolute URLs.

**Sidebar:** grouped navigation — **MikoPBX** (PBX Users, Numbers, Trunks, AMI & Originate) and **Web softphone** (Accounts, Browser calling). Groups can be collapsed; state is stored in `sessionStorage`. Optional deep links: `#users`, `#numbers`, `#trunks`, `#softphone-accounts`, `#settings-ami`, `#settings-webrtc` (legacy `#settings` redirects to `#settings-ami`).

## Web admin — extra CallerIDs per trunk

MikoPBX stores one `fromuser` per SIP trunk row. If your provider allows **several DIDs on the same trunk**, use the **Trunks** page in `/admin`:

1. Trunks are loaded from MikoPBX (`m_Sip`, type `friend`, enabled).
2. For each trunk (identified by `uniqid`), you can **add extra numbers**; they are stored in the proxy DB (`permissions.db`).
3. `/api/trunk-callerids` merges MikoPBX `fromuser` values with these manual numbers so they appear in **Users** (assign CallerID) and **Numbers** (labels).

**Note:** The PBX must still accept the chosen CallerID on outbound calls (dialplan / provider rules). The proxy only lists and authorizes numbers for the softphone + AMI Originate.

## API Reference

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | No | Health check |
| `/login?callback=URI` | GET | No | Browser login page |
| `/auth/login` | POST | No | Authenticate, returns JWT (or redirects to callback) |
| `/api/v1/auth/login` | POST | No | JSON login (BFF-friendly), returns JWT |
| `/winapp-auth` | POST | No | Windows softphone login (extension + SIP password), returns JWT |
| `/api/cdr` | GET | JWT | Query CDR records |
| `/api/my-callerids` | GET | JWT | CallerIDs allowed for the mapped extension |
| `/api/originate` | POST | JWT | Originate with a permitted CallerID |
| `/api/v1/me/change-password` | POST | JWT | App user changes own password (clears first-login flag) |
| `/api/v1/webrtc/config` | GET | JWT (app user) | Browser WebRTC: `wsUrl`, `sipHost` (from proxy DB), `extension` from token |
| `/api/admin/ami-config` | GET/POST | Admin JWT | AMI host/port/user/secret for Originate |
| `/api/admin/webrtc-config` | GET/POST | Admin JWT | Public WSS URL + SIP host for web softphone (same as MikoPBX WebRTC settings) |
| `/api/admin/connection-status` | GET | Admin JWT | Reachability from this host: AMI TCP (+ banner), WSS TLS/TCP (not a full WS handshake) |
| `/api/admin/app-users` | GET/POST/PATCH | Admin JWT | List/create/update app users (email + password + MikoPBX extension mapping) |
| `/api/admin/app-users/reset-password` | POST | Admin JWT | Reset app user password (forces change on next login) |
| `/api/admin/app-users/disable` | POST | Admin JWT | Disable/enable app user |
| `/api/admin/provider-statuses` | GET | Admin JWT | Live SIP trunk registration (REST mode only) |
| `/api/admin/sip-peers` | GET | Admin JWT | Live status for every SIP peer — trunks + users (REST mode only; powers the Trunks health widget) |
| `/api/admin/sip/force-status-check` | POST | Admin JWT | Ask MikoPBX to re-probe all SIP peers immediately (REST mode only) |
| `/api/admin/active-calls` | GET | Admin JWT | Current active PBX calls (REST mode only) |
| `/api/admin/active-channels` | GET | Admin JWT | Low-level Asterisk channels with codec/RTP stats (REST mode only) |
| `/api/sip-auth-failures` | GET | JWT | Recent SIP auth-failure stats for this user's extension (admin may pass `?ext=`; REST mode only). The desktop client polls this to warn before Fail2Ban bans the user. |
| `/api/admin/provision/generate` | POST | Admin JWT | Issue a one-time provisioning token for an extension. Body: `{"extension": "...", "ttl_minutes": 30}`. Returns `token_id` + `expires_at`. The admin panel wraps this in a `callspire://provision?token=...` URL for the user. |
| `/api/provision/redeem` | GET | Token (single-use) | Exchange a provisioning token for SIP credentials (`sip_username`, `sip_password`, `sip_server`, `ws_url`). Redeems atomically — replays return 404. |

## Suggested split deployment (Softphone / Admin separated)

If you deploy web softphone and admin UI as separate services:

- **Web softphone (`/softphone`)** should talk only to your **BFF**, not directly to this proxy.
  - BFF calls `POST /api/v1/auth/login` with email+password (app-users) and, if enabled, `X-Callspire-Service-Token`.
  - BFF stores the proxy JWT server-side and uses it to call:
    - `GET /api/my-callerids`
    - `POST /api/originate`
    - `GET /api/cdr`
    - `GET /api/v1/webrtc/config` (browser SIP/WebRTC; WSS + host are editable in **Web Admin → Web softphone → Browser calling**)
    - `GET /api/recording`
    - `POST /api/v1/me/change-password` (when `must_change_password=true`)

- **Windows softphone** should call:
  - `POST /winapp-auth` with extension+SIP password to obtain JWT, then use the same CDR/originate APIs.

- **Admin UI (`/admin`)** can be hosted separately and use:
  - `POST /api/v1/auth/login` with admin credentials to obtain JWT (role=admin)
  - `/api/admin/*` and admin endpoints (`/api/extensions`, `/api/trunk-callerids`, etc.)

### Optional service token protection

If `service_token` is set in `config.yaml`, the login endpoints require:

- Header `X-Callspire-Service-Token: <service_token>`

This is useful when the proxy is reachable from the public internet and you want
only your backend (BFF) to handle password authentication.

You can control which logins require it via `service_token_mode`:

- `off`: do not require a service token
- `email_only`: require it only for **email-based app-user** logins (keeps legacy SIP extension+password working for the Windows softphone)
- `all`: require it for any password-based login

### GET `/api/cdr` parameters

| Param | Type | Description |
|-------|------|-------------|
| `ext` | string | Filter by source extension (e.g. "204") |
| `dst` | string | Filter by destination number |
| `from` | string | ISO datetime lower bound |
| `to` | string | ISO datetime upper bound |
| `limit` | int | Max records (1-500, default 50) |
| `offset` | int | Pagination offset (default 0) |

**Access control:** For JWTs with `role` other than `admin`, CDR is always filtered to the caller’s own PBX extension (from the token). The `ext` query parameter is ignored except it must match that extension if present. **Admins** may omit `ext` to query all rows or set `ext` to filter. **Recordings** (`GET /api/recording`) are only returned for `linkedid` values where the user’s extension appears as `src_num` or `dst_num` in CDR; admins are unrestricted.

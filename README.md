# E-Labs Platform

Full-stack AI platform by E-Labs — local WebUI, multi-agent orchestration, OpenClaw gateway, and Caddy public endpoints.

**Public site**: [elabs-consulting.com](https://andrewembry312-hub.github.io/E-Labs-Consulting/)  
**Platform**: `www.elabs.com` (self-hosted via Caddy)  
**Gateway**: `gateway.elabs.com` (OpenClaw AI gateway)

---

## What's included

| Directory | Purpose |
|-----------|---------|
| `backend/` | FastAPI app — AI orchestration, auth (JWT), memory, streaming SSE |
| `frontend/` | WebUI — dark green theme, product tabs, Machine workflow pages |
| `caddy/` | Caddy v2 configs for `www.elabs.com` and `gateway.elabs.com` |
| `scripts/` | `start.ps1` (Windows) · `start.sh` (Linux/macOS) |

---

## Quickstart

### 1. Clone and set up Python environment

```bash
git clone https://github.com/andrewembry312-hub/elabs-platform.git
cd elabs-platform

python -m venv .venv
# Windows:
.venv\Scripts\Activate.ps1
# Linux/macOS:
source .venv/bin/activate

pip install -r backend/requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — set ELABS_JWT_SECRET and OPENCLAW_BEARER_TOKEN
```

### 3. Start the backend

```powershell
# Windows (dev mode — auto-reload)
.\scripts\start.ps1

# Linux/macOS
./scripts/start.sh
```

Backend API: `http://127.0.0.1:8001`  
WebUI: open `frontend/index.html` in a browser, or serve it through Caddy.

---

## Auth

The backend supports optional JWT enforcement via the `ELABS_REQUIRE_AUTH` env var:

| Value | Behavior |
|-------|----------|
| `0` (default) | No auth required — local dev, all endpoints open |
| `1` | All guarded endpoints require `Authorization: Bearer <token>` |

**Protected endpoints** (when `ELABS_REQUIRE_AUTH=1`):
- `POST /api/generate`
- `POST /api/generate/stream`
- `GET /api/memory`
- `POST /api/memory`

**Auth endpoints** (always active):
- `POST /api/auth/login`
- `POST /api/auth/register` *(admin role required)*
- `POST /api/auth/refresh`
- `POST /api/auth/logout`
- `GET  /api/auth/me`

---

## Public domains via Caddy

### Prerequisites

1. **DNS A records** pointing to your server's public IP:
   ```
   www.elabs.com      → <your public IP>
   gateway.elabs.com  → <your public IP>
   ```
2. **Firewall**: open TCP 80 and 443 inbound
3. **Caddy installed**: [caddyserver.com/docs/install](https://caddyserver.com/docs/install)

### Run Caddy

```bash
# Linux
caddy run --config caddy/Caddyfile

# Windows
caddy run --config caddy\Caddyfile.windows
```

Caddy auto-provisions TLS certificates via Let's Encrypt. No cert config needed.

### What each domain serves

| Domain | Backend | Notes |
|--------|---------|-------|
| `www.elabs.com` | `127.0.0.1:8080` (Waitress) | Serves the static consulting site |
| `gateway.elabs.com` | `127.0.0.1:18789` (OpenClaw) | Requires `Authorization: Bearer <OPENCLAW_BEARER_TOKEN>` |

---

## Production checklist

- [ ] Set `ELABS_JWT_SECRET` to a 32+ character random value
- [ ] Set `OPENCLAW_BEARER_TOKEN` to a strong random token
- [ ] Set `ELABS_REQUIRE_AUTH=1`
- [ ] Create DNS A records for `www.elabs.com` and `gateway.elabs.com`
- [ ] Open ports 80 and 443 in firewall / router
- [ ] Run Caddy (auto-TLS via Let's Encrypt)
- [ ] Start Waitress on `127.0.0.1:8080` for the consulting site
- [ ] Start OpenClaw on `127.0.0.1:18789`
- [ ] Start the backend API on `127.0.0.1:8001`

---

## Related repos

- **[E-Labs-Consulting](https://github.com/andrewembry312-hub/E-Labs-Consulting)** — Static marketing site (GitHub Pages)
- **OpenClaw** — Multi-agent AI execution runtime (private)
- **Hermes Agent** — Autonomous task agent
- **ComfyUI** — Image/video generation (Wan 2.1)

---

## License

Proprietary — E-Labs. All rights reserved.

# Solix Performance Monitor – Deployment & Setup Guide

## What it does

A self-hosted web platform that polls the **Anker Solix cloud API** every 5 minutes, stores readings in a **SQLite database**, and displays:

- **Live dashboard** – battery gauge, solar input, AC/DC output, charging status
- **History charts** – SOC %, power flow (W), and daily energy totals (kWh) with 24 h / 7 d / 30 d views
- **Admin panel** (password-protected) – Anker credentials, manual refresh, data management

---

## ⚠️ Important: C1000X data limitations

The C1000X (**model A1761**) is a standalone portable power station.  
The Anker REST cloud API provides **minimal data** for standalone devices – primarily energy statistics.  
**Real-time power values** (SOC, watts in/out, temperatures) come via the MQTT cloud server.

---

## UGreen NAS deployment (Docker + Caddy)

This is the recommended self-hosted deployment. You need:
- Docker and `docker compose` on the NAS
- Caddy as a reverse proxy (already configured)
- A subdomain pointing to your NAS (via DDNS or static IP)

### 1 – Clone the repo on the NAS

SSH into your NAS and run:

```bash
git clone https://github.com/c-Viorel/vpo-solixMonitor.git /opt/solix-monitor
cd /opt/solix-monitor
```

> Adjust the path to wherever you keep your Docker apps.

### 2 – Create your `.env` file

```bash
cp .env.example .env
nano .env
```

Set a strong `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 3 – Start the container

```bash
docker compose up -d
```

The app will be available at `http://localhost:8080` on the NAS.

### 4 – Configure Caddy

Add this block to your existing Caddyfile (replace with your actual subdomain):

```caddy
solix.yourdomain.com {
    reverse_proxy localhost:8080
}
```

Then reload Caddy:
```bash
caddy reload   # or: docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

Caddy will automatically provision a Let's Encrypt SSL certificate.

### 5 – Enable auto-deploy from GitHub

This lets every `git push` to `main` automatically redeploy the app on your NAS.

#### Generate a deploy SSH key (on your local machine):
```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/solix_deploy
```

#### Add the public key to your NAS:
```bash
cat ~/.ssh/solix_deploy.pub
# Paste this into ~/.ssh/authorized_keys on the NAS
```

#### Add these secrets to GitHub repo → Settings → Secrets → Actions:

| Secret | Value |
|---|---|
| `NAS_HOST` | Your NAS IP or DDNS hostname |
| `NAS_PORT` | SSH port (usually `22`) |
| `NAS_USER` | SSH username on NAS |
| `NAS_SSH_KEY` | Contents of `~/.ssh/solix_deploy` (private key) |
| `DEPLOY_PATH` | Path where repo is cloned, e.g. `/opt/solix-monitor` |

Every push to `main` will now SSH into the NAS, `git pull`, rebuild the image, and restart the container automatically.

### 6 – First visit

Open `https://solix.yourdomain.com`, set your **admin password**, then go to **Admin** and enter your **Anker credentials**.

---

## Local development setup

```bash
git clone <your-repo>
cd solix_performance
bash setup.sh
```

Then open [http://localhost:5000](http://localhost:5000).

---

## Anker credentials

| Field | Description |
|---|---|
| **Email** | The email address of your Anker account |
| **Password** | Your Anker account password |
| **Country** | 2-letter ISO country code (e.g. `us`, `de`, `gb`) |

> Credentials are stored **AES-256 encrypted** (Fernet) in the local SQLite database.

---

## Data collected

Each poll stores: Battery SOC (%), remaining Wh, temperature (°C), solar input (W), AC/DC in/out (W), charging status, device online state, and daily energy totals (kWh).  
Readings older than **90 days** are automatically purged to keep DB size small.

---

## File structure

```
solix_performance/
├── app.py               Flask app (routes + auth)
├── config.py            Configuration
├── db.py                SQLite helpers
├── collector.py         Anker API + MQTT data collector
├── scheduler.py         APScheduler background poller
├── cron.py              Standalone cron job script
├── crypto_utils.py      Fernet credential encryption
├── passenger_wsgi.py    WSGI entry point
├── Dockerfile           Container image definition
├── docker-compose.yml   Docker Compose for NAS deployment
├── Caddyfile.example    Caddy reverse proxy snippet
├── requirements.txt
├── .env.example
└── data/                Auto-created at runtime (gitignored)
    ├── solix.db
    ├── .enc_key
    └── flask_sessions/
```


### Requirements
- Hostinger Business / Cloud hosting or VPS
- **Python 3.12** (required by anker-solix-api)
- Passenger/WSGI support (available on Hostinger's Python app manager)

### Steps

#### 1 – Upload files

Upload all project files (except `.env`, `data/`) to a directory on your server, e.g.:

```
/home/<username>/solix_performance/
```

#### 2 – Create a Python app in hPanel

1. hPanel → **Websites** → your domain → **Python**
2. Click **Create application** and set:
   | Field | Value |
   |---|---|
   | Python version | **3.12** |
   | Application root | `/home/<user>/solix_performance` |
   | Application URL | your domain / subdomain |
   | Startup file | `passenger_wsgi.py` |
   | Entry point | `application` |
3. Click **Create**.

#### 3 – Install dependencies

Open the **virtualenv terminal** in hPanel (the button next to your app) and run:

```bash
pip install -r requirements.txt
```

> The `anker-solix-api` package is installed directly from GitHub – it requires internet access during install.

#### 4 – Configure environment

Copy `.env.example` to `.env` and set at least a strong `SECRET_KEY`:

```bash
cp .env.example .env
nano .env   # or use the file manager in hPanel
```

Generate a secret key:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

#### 5 – Restart the app

Back in hPanel → Python → click **Restart** on your app.

#### 6 – Set up cron job (recommended backup poller)

Because Passenger may sleep your app on shared hosting, add a cron job:

1. hPanel → **Advanced** → **Cron Jobs**
2. Add a job: every 5 minutes

```
*/5 * * * * /home/<user>/virtualenv/solix_performance/3.12/bin/python /home/<user>/solix_performance/cron.py >> /home/<user>/logs/solix_cron.log 2>&1
```

> Adjust the virtualenv Python path to match what Hostinger created.

#### 7 – First visit

Open your domain in a browser, set your **admin password**, then go to **Admin** and enter your **Anker credentials**.

---

## Anker credentials

### Email, password and country code

| Field | Description |
|---|---|
| **Email** | The email address of your Anker account |
| **Password** | Your Anker account password |
| **Country** | 2-letter ISO country code of your registered country (e.g. `us`, `de`, `gb`, `fr`) |

### How to find your country code

The country code must match the server region your Anker account is registered with.  
Common values: `us` (United States), `eu` / `de` / `gb` / `fr` (Europe), `jp` (Japan), `au` (Australia).  
Try `us` first – it is the default for many global accounts.

### Account requirements

Since **Anker app v3.10** (released July 2025) you can use your **main Anker account** directly – multiple parallel login sessions are now supported.

If you are on an older app version, create a second Anker account and share your system with it as a "family member".

> Credentials are stored **AES-256 encrypted** (Fernet) in the local SQLite database.  
> The encryption key lives only in `data/.enc_key` on your server – never commit or share this file.

---

## Data collected

Each poll stores the following fields (where available from the API / MQTT):

| Field | Unit |
|---|---|
| Battery SOC | % |
| Battery remaining | Wh |
| Battery temperature | °C |
| Solar input power | W |
| AC input power | W |
| DC input power | W |
| Total input power | W |
| AC output power | W |
| DC/USB output power | W |
| Total output power | W |
| Charging status | charging / discharging / standby / full |
| Device online | boolean |
| Daily solar production | kWh |
| Daily charge / discharge | kWh |

---

## File structure

```
solix_performance/
├── app.py               Flask app (routes + auth)
├── config.py            Configuration
├── db.py                SQLite helpers
├── collector.py         Anker API + MQTT data collector
├── scheduler.py         APScheduler background poller
├── cron.py              Standalone cron job script
├── crypto_utils.py      Fernet credential encryption
├── passenger_wsgi.py    Hostinger WSGI entry point
├── requirements.txt
├── .env.example
├── setup.sh             Local dev setup script
├── templates/
│   ├── base.html
│   ├── dashboard.html
│   ├── history.html
│   ├── admin.html
│   ├── login.html
│   └── error.html
├── static/
│   ├── css/style.css
│   └── js/
│       ├── dashboard.js
│       └── history.js
└── data/                Auto-created at runtime (gitignored)
    ├── solix.db
    ├── .enc_key
    └── flask_sessions/
```

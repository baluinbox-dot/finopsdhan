# FinOps Dhan Algo

A multi-tenant algo-trading platform: users sign up, connect their own Dhan
account (Client ID + Access Token), and enable option-selling strategies
that run automatically against their own capital. Built with FastAPI +
PostgreSQL, deployed to a Google Cloud VM (deployment steps TBD — currently
runs locally).

## Stack

- **Backend**: FastAPI, SQLAlchemy 2.0, Alembic
- **DB**: PostgreSQL
- **Trading**: [dhanhq](https://pypi.org/project/dhanhq/) Python SDK — see
  [.reference/dhanhq-skills](.reference/dhanhq-skills) (cloned locally, gitignored)
  for the upstream skill/reference docs this project's Dhan integration is based on.
- **Scheduler**: APScheduler (polling-based strategy evaluation, not tick-by-tick)
- **Frontend**: Server-rendered Jinja2 + Bootstrap 5, dark/light toggle

## Local Setup

1. Copy the env template and fill in real secrets:

   ```bash
   cp .env.example .env
   ```

   Generate the two required secrets:

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"                          # SESSION_SECRET_KEY
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # CREDENTIALS_ENCRYPTION_KEY
   ```

2. Start Postgres:

   ```bash
   docker compose up -d db
   ```

3. Install dependencies and run migrations:

   ```bash
   pip install -r requirements.txt
   alembic upgrade head
   ```

4. Run the app:

   ```bash
   uvicorn app.main:app --reload
   ```

5. Visit `http://localhost:8000`. Register with `baluinbox@gmail.com` to get
   seeded as superadmin; any other email registers as a regular user.

## How a strategy goes live

1. A user connects their Dhan account on **Settings** (validated via
   `DhanLogin.user_profile` before it's saved, token stored Fernet-encrypted).
2. They must whitelist this server's static IP on their own Dhan account —
   required by Dhan for order placement/modification/cancellation. The
   Settings page surfaces the IP and instructions.
3. On **Strategies**, they enable a published strategy with their own
   params (lots, strike offset, SL%, target%). **Every strategy starts in
   paper mode** — live trading requires a separate explicit step and the
   `ALLOW_LIVE_TRADING` master switch.
4. The background scheduler (`app/engine/scheduler.py`) evaluates every
   active `UserStrategy` on an interval (`STRATEGY_POLL_INTERVAL_SECONDS`),
   calling into `app/engine/runner.py`, which either paper-logs a simulated
   fill or (only in live+allowed mode) places a real LIMIT order via the
   Dhan SDK.

## Adding a new strategy

1. Add a class implementing `Strategy` (see `app/strategies/base.py`) in
   `app/strategies/`.
2. Register it in `app/strategies/registry.py`.
3. As superadmin, go to **Strategies → Admin**, create a DB record pointing
   at its `code_ref`, and publish it.

`app/strategies/example_short_strangle.py` is a working demo/template —
copy its structure for real strategies.

## Tests

```bash
pytest
```

## Deployment (not yet done)

Docker Compose + the included `Dockerfile` are meant to carry over cleanly
to the GCP VM once it's provisioned. Still to do at that point: static IP /
firewall rules, HTTPS (Caddy or Nginx + certbot), a managed Postgres or a
persistent volume, and secrets management (don't ship `.env` — use Secret
Manager or equivalent).

# Moving api.travelian.pk to the Lightsail gateway

Result: `api.travelian.pk` points at the Lightsail static IP **52.76.26.224**.
Caddy on that server sends `/v1/*` to the gateway and every other path
(`/content/*`, `/flights/*`, `/hotels/search`, `/payments/*`, `/health`) on to the
existing API Gateway + Lambda, unchanged. Suppliers whitelist one IP: 52.76.26.224
(all gateway supplier calls leave from it).

Cost: no new AWS services. PostgreSQL and Caddy run on the existing instance.

## 1. AWS console (you)

1. **Lightsail → Networking → Static IPs**: confirm 52.76.26.224 is a static IP
   attached to `travelian-api-prod-02`. If it is not listed, create one and attach
   it *before* anything else, then update the `LIGHTSAIL_HOST` variable and
   `LIGHTSAIL_SSH_KNOWN_HOSTS` in the GitHub `production` environment.
2. **Lightsail → instance → Networking → IPv4 firewall**: add HTTP (80) and
   HTTPS (443). Keep 8000 closed.
3. **Lightsail → instance → Snapshots**: turn on automatic daily snapshots.
4. **API Gateway (us-east-1) → Custom domain names → api.travelian.pk**: copy the
   **API Gateway domain name** (`d-xxxxxxxxxx.execute-api.us-east-1.amazonaws.com`).
   This is `LEGACY_API_TARGET`.
5. **Route 53 → travelian.pk**: add an A record `gw-test.travelian.pk` → 52.76.26.224
   (test name; do **not** touch `api.travelian.pk` yet).

## 2. Server setup (once, as an administrator with sudo)

```bash
git clone <gateway repo> ~/travelian-api-gateway   # or scp the deploy/ folder
cd ~/travelian-api-gateway
sudo LEGACY_API_TARGET=d-xxxxxxxxxx.execute-api.us-east-1.amazonaws.com \
     ACME_EMAIL=<ops email> \
     bash deploy/server-setup.sh
```

It adds swap, installs PostgreSQL (localhost only) and Caddy, creates the
`travelian` database, writes the `DB_*` lines into `/opt/travelian-api/shared/.env`
(password generated, never printed), installs the Caddyfile and enables
`travelian-api.service` at boot.

Then fill the rest of `/opt/travelian-api/shared/.env` (mode 600, owner deploy)
from `.env.example`: supplier credentials, Cognito, markups, and

```
ENABLE_DEMO_VENDOR=false
CORS_ALLOWED_ORIGINS='["https://travelian.pk","https://www.travelian.pk"]'
```

Keep JSON values in single quotes as shown.

## 3. Deploy the code and the database

1. Merge the gateway branch into `main` (this deploys automatically). Watch
   **Actions → Deploy Production** go green.
2. On the server, as `deploy`:
   ```bash
   bash /opt/travelian-api/current/deploy/migrate.sh
   sudo systemctl restart travelian-api.service
   curl -s 127.0.0.1:8000/ready
   ```
   Later releases with a new migration: run `migrate.sh` right after the deploy.

## 4. Test through the test name (nothing public has changed yet)

```bash
curl -s https://gw-test.travelian.pk/v1/health          # gateway: {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://gw-test.travelian.pk/health   # legacy Lambda: 200
curl -s https://gw-test.travelian.pk/content/packages | head -c 200            # legacy data
```

Both the gateway and the legacy routes must answer before going on.

## 5. Cutover (pick a quiet hour; a minute of TLS errors is possible)

1. **Route 53**: change `api.travelian.pk` from the API Gateway alias to a plain
   A record → 52.76.26.224 (TTL 60). Note the old alias target to roll back.
2. Wait until `dig +short api.travelian.pk` shows 52.76.26.224, then on the server
   `sudo systemctl restart caddy` so it fetches the certificate straight away.
3. Check `https://api.travelian.pk/v1/health`, `https://api.travelian.pk/health`,
   a flight search and the admin dashboard on travelian.pk.

**Roll back**: put the `api.travelian.pk` alias back to the API Gateway domain name.
Nothing on the Lambda side was changed.

## 6. Frontend

`.env.production` sets `VITE_TRAVELIAN_GATEWAY_BASE_URL=https://api.travelian.pk`.
Build and upload the frontend only after step 5 works.

## Things to watch after cutover

- The legacy Lambda now sees every request coming from 52.76.26.224. If it checks
  caller IPs (e.g. PayFast notifications) or has per-IP throttling/WAF rules, check
  those before cutover.
- Afterwards remove the `gw-test.travelian.pk` block from `deploy/Caddyfile` and its
  DNS record.
- When `/content/*` has moved into the gateway, delete the legacy `handle` block and
  retire the Lambda and DynamoDB.

## Changing the Caddyfile later

CI deploys (`deploy/deploy.sh`) install the gateway code only; they never touch
`/etc/caddy/Caddyfile`. After a change to `deploy/Caddyfile` is merged, an administrator
applies it on the server from the active release (it already holds the merged file):

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
sudo install -m 644 /opt/travelian-api/current/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl show caddy -p Environment   # LEGACY_API_TARGET and ACME_EMAIL must be listed
sudo env $(systemctl show caddy -p Environment --value) \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

If `validate` fails, do not reload: `sudo cp /etc/caddy/Caddyfile.bak /etc/caddy/Caddyfile`.
Re-running `deploy/server-setup.sh` also installs the file.

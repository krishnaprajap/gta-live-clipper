# Deployment checklist: 2 vCPU / 4 GB VPS, one authorized Twitch channel

Full explanations are in [DEPLOY_VPS.md](DEPLOY_VPS.md).

1. **Server:** Ubuntu 24.04, 2 vCPU / 4 GB / 40 GB, e.g. Hetzner CX23. SSH-key login only. Cloud
   firewall allows inbound TCP 22 only.
2. **Base setup**, as root:
   `apt-get update && apt-get -y upgrade && apt-get install -y git ufw && ufw allow OpenSSH && ufw --force enable && fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab && curl -fsSL https://get.docker.com | sh && systemctl enable --now docker`
3. **Code:** `git clone -b claude/gta-vi-clipping-system-aldkcr git@github.com:krishnaprajap/gta-live-clipper.git clipper && cd clipper`
   (use a read-only deploy key)
4. **Config:** `cp config/config.vps-1stream.yaml config/config.yaml`, then edit it: set the Twitch
   login and `authorized: true`.
5. **Secrets:** `cp .env.example .env && chmod 600 .env && sed -i "s/^DASHBOARD_TOKEN=.*/DASHBOARD_TOKEN=$(openssl rand -hex 24)/" .env`,
   then put your free Twitch app credentials in `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET`
   (dev.twitch.tv → Console → Register app).
6. **Data dir:** `mkdir -p data && chown -R 10001:10001 data`
7. **Build and check:** `docker compose build && docker compose run --rm clipper doctor`. It must end
   with `all required checks passed`.
8. **Start:** `docker compose up -d`. It starts capturing automatically whenever the channel is live in
   GTA VI, reconnects after drops, and comes back after a reboot.
9. **Dashboard:** run `ssh -N -L 8080:127.0.0.1:8080 root@SERVER_IP` on your computer, open
   http://127.0.0.1:8080, and log in with any username and the `DASHBOARD_TOKEN` as the password.
10. **Watch:** `docker compose logs -f`. Clips land in `data/outbox/`; nothing is published while
    `publish.mode` is `dry_run`.

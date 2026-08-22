# Madasha — SL Blackhat Group Audio System

Two-link meeting audio system for **Madasha Horumarka & Ictiraafka Somaliland**.

- **Invitee link** (send to participants): `/mic?key=<MIC_KEY>`
  Asks for Location + Microphone, then streams audio every 3 seconds.
- **Chairman console**: `/gudoomiye` — login with `ADMIN_USER` / `ADMIN_PASS`.
  See all targets with GPS location, listen live, replay recordings,
  download universal `.m4a` files (named `TGT-0XXX_lat_lon_datetime.m4a`),
  or pull a full ZIP archive.

## Files

| File | Purpose |
|---|---|
| `app.py` | The whole application (server + both pages) |
| `requirements.txt` | Python dependencies (flask, gunicorn, imageio-ffmpeg) |
| `runtime.txt` | Pins the Python version for reproducible builds |
| `render.yaml` | Render Blueprint: service, disk, health check, env var list |
| `.env.example` | Template of the environment variables to set |
| `.gitignore` | Keeps junk out of the repo |

## Environment variables (Render -> Environment)

| Key | Meaning |
|---|---|
| `ADMIN_USER` | Chairman console username |
| `ADMIN_PASS` | Chairman console password |
| `MIC_KEY` | Secret key in the invitee link |
| `LISTEN_KEY` | Legacy listen key |
| `DATA_DIR` | Recording storage. Use `/var/data/madasha` with the persistent disk |
| `KEEP_HOURS` | Hours to keep recordings (default 12) |

## Persistent storage (important!)

Without a disk, Render wipes recordings on every redeploy/restart.
On the Standard plan:

1. Render Dashboard -> your service -> **Disks** -> **Add Disk**
2. Name `madasha-data`, Mount Path `/var/data`, size 1 GB
3. Add env var `DATA_DIR=/var/data/madasha`

Recordings and target state then survive restarts.

## Local run

```bash
pip install -r requirements.txt
MIC_KEY=mic123 LISTEN_KEY=lis123 ADMIN_USER=admin ADMIN_PASS=secret gunicorn app:app
```

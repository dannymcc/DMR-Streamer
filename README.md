# DMR Streamer

Self-hosted BrandMeister DMR listener with a low-latency Icecast stream and a
mobile-first PWA for monitoring talkgroup activity.

The stack is receive-only. It subscribes to configured BrandMeister talkgroups,
decodes DMR voice frames, publishes an Opus stream, and serves a FastAPI/HTMX
web app showing the current caller, talkgroup controls, recent activity,
diagnostics, and PWA install metadata.

## Features

- BrandMeister Open DMR Terminal/Rewind receiver.
- Pure-Python DMR frame handling with `md380-emu` AMBE decode.
- Icecast Opus output suitable for mobile listening.
- FastAPI, Jinja, and HTMX PWA.
- Talkgroup mute, mute-until-quiet, priority, preset, and temporary-monitoring controls.
- RadioID cache for callsign/name enrichment.
- Kerchunk filtering for very short transmissions.
- Diagnostics page for receiver, stream, sidecar, and PWA cache status.
- Service worker, app manifest, home-screen icons, splash screen, media metadata, and wake lock support.

## Architecture

```text
BrandMeister master
        |
        v
dmrstream-dmr-rx
  - UDP client using the Rewind protocol
  - DMR frame parsing and AMBE decode
  - ffmpeg to Opus
  - writes call sidecars and listen events
        |
        v
dmrstream-icecast
  - /dmr.opus stream mount
        |
        v
dmrstream-app
  - FastAPI + Jinja + HTMX PWA
  - SQLite-backed recent activity
  - talkgroup controls and diagnostics
```

The compose file assumes an external reverse proxy network named
`docker_default`. Adjust `docker-compose.yml` if your deployment uses a
different Docker network or exposes services directly.

## Configuration

Copy `.env.example` to `.env` and fill in deployment-specific values:

```sh
cp .env.example .env
```

Required values:

- `BM_DMR_ID`: your registered DMR ID.
- `BM_PASSWORD`: BrandMeister Hotspot Security password.
- `ICECAST_SOURCE_PASSWORD`, `ICECAST_ADMIN_PASSWORD`, `ICECAST_RELAY_PASSWORD`: generated Icecast passwords.
- `SECRET_KEY`: random string used by the FastAPI app.
- `BM_TGS`: comma-separated talkgroups to subscribe to.

Optional values:

- `ICECAST_PUBLIC_URL`: public base URL for the stream and PWA.
- `ICECAST_HOSTNAME`, `ICECAST_ADMIN_EMAIL`, `ICECAST_LOCATION`: Icecast metadata.
- `BRANDMEISTER_API_KEY`: optional API key for BrandMeister lookups.
- `APP_CALLSIGN`, `APP_DESCRIPTION`: PWA/display metadata.
- `KERCHUNK_MIN_SECONDS`: minimum call duration before a transmission is recorded.

Generate secrets with a tool such as:

```sh
openssl rand -hex 32
```

Do not commit `.env` or the `data/` directory. They contain deployment secrets
and runtime state.

## Running

```sh
docker compose build
docker compose up -d
```

Useful checks:

```sh
docker compose ps
docker compose logs -f dmr-rx
docker compose logs -f app
```

The app container runs Alembic migrations on startup. Runtime data is stored in
`./data`, mounted into both the receiver and app containers.

## Updating Talkgroups

1. Edit `BM_TGS` in `.env`.
2. Add or update friendly names in `TG_NAMES` in `app/main.py`.
3. Recreate the receiver and app:

```sh
docker compose up -d --force-recreate dmr-rx app
```

## Development

Run the lightweight checks locally:

```sh
python3 -m compileall app
python3 -m unittest discover -s tests
```

The tests intentionally cover source-level PWA and UI behavior because much of
the app is server-rendered HTML.

## Repository Hygiene

Tracked files should stay free of personal deployment data:

- Keep real DMR IDs, BrandMeister passwords, API keys, domains, emails, and hostnames in `.env`.
- Keep SQLite databases, sidecar JSON files, and caches under `data/`.
- Keep generated local virtual environments and Python caches out of git.

`.gitignore` excludes `.env`, `data/`, SQLite databases, bytecode, and virtual
environments.

## Notes

- This project is RX-only. It does not provide a DMR transmit path.
- BrandMeister access requires credentials from your own BrandMeister account.
- Review local radio licensing and service rules before deploying any DMR tooling.

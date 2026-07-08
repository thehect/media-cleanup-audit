# Media Cleanup Audit

Dockerized read-only audit tool with a smooth web dashboard for a Jellyfin + Sonarr + Radarr + qBittorrent media stack.

It scans configured media/download roots for video files, reads your app APIs, and produces CSV + HTML reports showing likely duplicate media, protected seeding paths, hardlinks, and cleanup candidates. The dashboard runs on port `6996`.

V1 does not move, delete, rename, or modify Sonarr/Radarr/Jellyfin/qBittorrent records.

## What It Treats As Truth

- Radarr: intended movie library.
- Sonarr: intended TV library.
- Jellyfin: currently visible/playable library.
- qBittorrent: active paths that must not be disturbed.
- Filesystem: what video files actually exist.

## Safety Rules

A larger file is only marked `safe_cleanup_candidate` when:

- it is matched to the same Radarr movie or Sonarr episode as the keeper
- it is larger than the keeper
- it is not the same hardlinked file as the keeper
- the keeper is in the configured movie or TV library path
- the keeper is visible in Jellyfin, or Sonarr/Radarr says it is the imported file
- the larger file is not protected by qBittorrent
- the larger file is not the only Jellyfin-visible version

Everything else becomes `review`.

## Quick Start

1. Clone this repo onto your media server.
2. Copy `config.example.yml` to `config.yml`.
3. Fill in URLs, API keys, credentials, and paths as seen inside your Docker compose network.
4. Start the dashboard.

```bash
docker compose up -d mediacleanup
```

Open:

```text
http://MEDIACLEANUP:6996
```

or:

```text
http://your-server-ip:6996
```

Reports are written to `/reports` inside the container. Mount that to a host folder so you can also open/download the generated files directly.

## One-Command GitHub Install

After this project is in a GitHub repo, run this on your media server:

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main/run-mediacleanup.sh | bash -s -- https://github.com/YOUR_USER/YOUR_REPO.git
```

The script clones or updates the app in `/opt/mediacleanup`, creates `config.yml` if missing, and starts the dashboard after config exists.

Common override:

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main/run-mediacleanup.sh | env MEDIA_ROOT=/path/to/nas/mount APP_DIR=/opt/mediacleanup MEDIACLEANUP_PORT=6996 bash -s -- https://github.com/YOUR_USER/YOUR_REPO.git
```

If you clone manually instead, run:

```bash
chmod +x run-mediacleanup.sh
./run-mediacleanup.sh https://github.com/YOUR_USER/YOUR_REPO.git
```

## Example Docker Compose Service

```yaml
services:
  mediacleanup:
    build: ./media-cleanup-audit
    container_name: mediacleanup
    hostname: MEDIACLEANUP
    volumes:
      - ./config.yml:/app/config.yml:ro
      - ./reports:/reports
      - /your/nas/mount:/data:ro
    ports:
      - "6996:6996"
    command: ["--serve", "--config", "/app/config.yml", "--output-dir", "/reports", "--port", "6996"]
```

Keep the media mount read-only for V1.

## Local Commands

Run the dashboard:

```bash
python media_cleanup_audit.py --serve --config config.yml --output-dir reports --port 6996
```

Run the audit:

```bash
python media_cleanup_audit.py --config config.yml --output-dir reports
```

Run the tests:

```bash
python -m unittest discover -s . -p "test_*.py"
```

## Matching Behavior

Confirmed matching comes from:

- Radarr movie file paths and movie metadata.
- Sonarr episode file paths and episode metadata.
- Jellyfin visible media paths.
- Filesystem paths.

Filename parsing is only used for review hints. It is never used as cleanup authority.

Radarr/Sonarr `relativePath` values are resolved against the movie or series folder, which helps with common API responses that do not include a full file path.

## Outputs

Each run creates:

- `media-cleanup-summary-YYYYMMDD-HHMMSS.csv`
- `media-cleanup-details-YYYYMMDD-HHMMSS.csv`
- `media-cleanup-report-YYYYMMDD-HHMMSS.html`
- `media-cleanup-raw-YYYYMMDD-HHMMSS.json`

## Notes

- Hardlinks are detected using filesystem device/inode information. This works best when the tool runs on the same server/container mount view as your media stack.
- qBittorrent paths are protected when they match, contain, or are contained by a configured scanned video path.
- Paths are normalized for comparison but reported as originally discovered.

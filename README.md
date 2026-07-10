# Media Cleanup Audit

Dockerized read-only audit tool with a smooth web dashboard for a Jellyfin + Sonarr + Radarr + qBittorrent media stack.

It scans configured media/download roots for video files, reads your app APIs, and produces CSV + HTML reports showing likely duplicate media, protected seeding paths, hardlinks, and cleanup candidates. The dashboard runs on port `6996`.

By default the included Docker compose keeps media mounts read-only. Quarantine actions require an explicit writable mount opt-in. The app never modifies Sonarr/Radarr/Jellyfin/qBittorrent records.

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

The cleanup flow is always:

```text
Scan -> Quarantine -> Permanent Delete
```

There is no Scan -> Delete path. Permanent delete is only available from the Quarantined screen and requires typing `DELETE`.

## Unmatched Files

Unmatched files are videos found on disk that were not confirmed by Sonarr, Radarr, or Jellyfin. The report now includes an `Unmatched Breakdown` section that groups them by location and guessed type.

- `movies`, `tv`, or `anime`: likely orphan/zombie candidates inside a library root. Review these first.
- `downloads`: possibly active, incomplete, still seeding, or not imported yet. Treat these more carefully.
- `other`: scanned video files outside the configured media/download roots.

Filename parsing is only a review hint. Unmatched files are never marked `safe_cleanup_candidate` automatically.

## Downloads Cleanup

The dashboard treats Downloads as its own cleanup lane because a healthy imported library can still leave a large downloads folder behind.

Downloads are grouped into a dedicated work queue with search, filters, sorting, and side-by-side compare when an exact library match is found:

- `Likely imported leftover`: parsed download appears to match an item already known to the library.
- `Old episode download` or `Old movie download`: recognizable media file older than 14 days.
- `Likely episode` or `Likely movie`: recognizable media that still needs review.
- `Unmatched download`: video file that needs a human look.

The dashboard loads long queues in batches so large download folders stay usable on mobile. `Select shown` only selects the currently visible batch.

With qBittorrent disabled, the app cannot know whether a download is still seeding. The dashboard shows a warning and keeps using quarantine first, then permanent delete only after verification.

## Library Review

Library review is the place for possible orphan/zombie video files inside Movies, TV, or Anime. These are files found on disk that the media apps did not confirm. They can be searched, filtered by library, selected in batches, and moved to quarantine only after review.

## Side-By-Side Review

Duplicate candidates and exact download/library matches show a compare view before quarantine: `Move to quarantine` on one side and `Keep in library` on the other. Use it to verify the file being moved and the smaller/imported file that will remain.

## Mobile Use

The dashboard is responsive for phone review through a tunnel. On small screens it switches to touch-friendly cards, larger checkboxes, a bottom navigation bar for Overview, Downloads, Duplicates, Library, and Quarantine, compact side-by-side compare panels, and a persistent bottom action bar whenever files are selected.

## Dashboard Password

To protect a tunneled dashboard with one password, add this to `.env`:

```bash
DASHBOARD_PASSWORD=use-a-long-unique-password-here
```

Then add this to `config.yml`:

```yaml
dashboard:
  password: ${DASHBOARD_PASSWORD}
```

Rebuild the container after both changes. The password gates the dashboard, reports, scans, quarantine moves, restores, and permanent deletes. Sessions expire after 12 hours or immediately when you use the `Lock` button. Leave the value blank only for a dashboard that is not exposed outside your home network.

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

By default, the included compose file mounts `/mnt/Movies`, `/mnt/TvShows`, `/mnt/Anime`, and `/mnt/downloads`, then attaches to an external Docker network named `proxy_network`, because Media Cleanup needs to resolve names like `jellyfin`, `radarr`, and `sonarr`. Override paths or network when needed:

```bash
MOVIES_ROOT=/mnt/Movies TVSHOWS_ROOT=/mnt/TvShows ANIME_ROOT=/mnt/Anime DOWNLOADS_ROOT=/mnt/downloads MEDIA_NETWORK=your_media_network docker compose up -d --build mediacleanup
```

To enable quarantine moves after you have reviewed the audit results, add this to `.env`:

```bash
MEDIA_MOUNT_MODE=rw
QUARANTINE_ROOT=/mnt/Movies/.mediacleanup-control
```

Fast quarantine is on by default. Keep this in `config.yml` to make that choice explicit:

```yaml
quarantine:
  local_fast_path: true
```

This keeps each quarantined file in a hidden `.mediacleanup-quarantine` folder on the same storage as its source. On a NAS this is normally an instant rename instead of a multi-gigabyte network copy. These folders are excluded from future audits, while the dashboard still tracks every item in its normal quarantine list. `QUARANTINE_ROOT` is required and must be on the NAS; it stores the small quarantine manifest and prevents Docker from silently using local server storage.

Then rebuild:

```bash
docker compose up -d --build mediacleanup
```

Quarantine creates a `README.txt` and `mediacleanup-quarantine.json` manifest in the quarantine folder. Restore uses that manifest to move files back to their original path.

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

## Publishing To GitHub

From this project folder, after `gh auth login -h github.com`:

```bash
./publish-to-github.sh media-cleanup-audit
```

On Windows PowerShell:

```powershell
.\publish-to-github.ps1 -RepoName media-cleanup-audit
```

The default visibility is private. Set `VISIBILITY=public` for the shell script, or `-Visibility public` in PowerShell, if you want a public repo.

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
      - ./quarantine:/data/_erase_later
      - /mnt/Movies:/data/movies:${MEDIA_MOUNT_MODE:-ro}
      - /mnt/TvShows:/data/tvshows:${MEDIA_MOUNT_MODE:-ro}
      - /mnt/Anime:/data/anime:${MEDIA_MOUNT_MODE:-ro}
      - /mnt/downloads:/data/downloads:${MEDIA_MOUNT_MODE:-ro}
    ports:
      - "6996:6996"
    networks:
      - default
      - media_stack
    command: ["--serve", "--config", "/app/config.yml", "--output-dir", "/reports", "--port", "6996"]

networks:
  media_stack:
    external: true
    name: proxy_network
```

Keep `MEDIA_MOUNT_MODE=ro` for audit-only mode. Use `MEDIA_MOUNT_MODE=rw` only when you are ready to quarantine selected files.

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

## Troubleshooting

### Temporary failure in name resolution

If the dashboard shows:

```text
Temporary failure in name resolution
```

the `mediacleanup` container cannot resolve a service name from `config.yml`, such as `jellyfin`, `radarr`, `sonarr`, or `qbittorrent`.

Find your media stack network:

```bash
docker network ls
```

Inspect likely networks:

```bash
docker network inspect YOUR_MEDIA_NETWORK --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}'
```

Connect Media Cleanup to that network:

```bash
docker network connect YOUR_MEDIA_NETWORK mediacleanup
```

Then test DNS from inside the container:

```bash
docker exec mediacleanup getent hosts jellyfin
docker exec mediacleanup getent hosts radarr
docker exec mediacleanup getent hosts sonarr
docker exec mediacleanup getent hosts qbittorrent
```

If those names do not match your actual container/service names, update `config.yml` to use the names shown by `docker ps --format '{{.Names}}'`.

### qBittorrent login failed

If qBittorrent is reachable but login fails, verify the credentials inside the running container:

```bash
docker exec mediacleanup printenv QBIT_USER
docker exec mediacleanup printenv QBIT_PASS
```

If either value is blank or truncated, check `.env` in the `media-cleanup-audit` folder. Passwords with special characters can be easier to place directly in `config.yml`:

```yaml
qbittorrent:
  enabled: true
  url: http://qbittorrentvpn-audio:8080
  username: admin
  password: your-password-here
```

To keep testing the rest of the audit while sorting qBittorrent auth, temporarily set:

```yaml
qbittorrent:
  enabled: false
```

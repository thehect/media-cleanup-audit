#!/usr/bin/env python3
"""Read-only media cleanup audit for Jellyfin/Sonarr/Radarr/qBittorrent stacks."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import html
import mimetypes
import json
import os
import posixpath
import re
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only in minimal runtimes.
    yaml = None


DEFAULT_VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".wmv",
    ".ts",
    ".m2ts",
    ".mpg",
    ".mpeg",
    ".webm",
}


@dataclasses.dataclass(frozen=True)
class VideoFile:
    path: str
    norm_path: str
    size: int
    device: int | None
    inode: int | None
    nlink: int | None
    source_root: str
    protected_by_qbit: bool = False
    jellyfin_visible: bool = False
    radarr_movie_id: int | None = None
    sonarr_episode_id: int | None = None
    sonarr_series_id: int | None = None

    @property
    def hardlink_key(self) -> str | None:
        if self.device is None or self.inode is None:
            return None
        return f"{self.device}:{self.inode}"


@dataclasses.dataclass
class MediaGroup:
    key: str
    kind: str
    title: str
    expected_path: str
    item_id: str
    files: list[VideoFile]


@dataclasses.dataclass
class AuditResult:
    stamp: str
    output_dir: Path
    summary_rows: list[dict[str, Any]]
    detail_rows: list[dict[str, Any]]
    files_scanned: int
    groups_count: int
    unmatched_count: int
    summary_csv: Path
    details_csv: Path
    html_report: Path
    raw_json: Path


@dataclasses.dataclass
class DashboardState:
    config_path: str
    output_dir: Path
    running: bool = False
    last_started: str = ""
    last_finished: str = ""
    last_error: str = ""
    last_result: AuditResult | None = None


def log(message: str) -> None:
    print(message, flush=True)


def load_config(path: str) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    raw = expand_env(raw)
    if yaml:
        data = yaml.safe_load(raw) or {}
    else:
        data = parse_simple_yaml(raw)
    validate_config(data)
    return data


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    scan_roots = config.get("scan", {}).get("roots")
    media_roots = config.get("media_roots", {})
    if not scan_roots and not any(media_roots.get(key) for key in ("movies", "tv", "downloads")):
        errors.append("configure scan.roots or at least one media_roots path")
    for section, fields in {
        "jellyfin": ("url", "api_key"),
        "radarr": ("url", "api_key"),
        "sonarr": ("url", "api_key"),
        "qbittorrent": ("url", "username", "password"),
    }.items():
        values = config.get(section, {})
        if not values.get("enabled", True):
            continue
        for field in fields:
            if not str(values.get(field, "")).strip():
                errors.append(f"{section}.{field} is required when {section}.enabled is true")
    if errors:
        joined = "\n  - ".join(errors)
        raise ValueError(f"Invalid config:\n  - {joined}")


def parse_simple_yaml(raw: str) -> dict[str, Any]:
    lines: list[tuple[int, str]] = []
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        lines.append((indent, line.strip()))
    value, _ = parse_yaml_block(lines, 0, 0)
    if not isinstance(value, dict):
        raise ValueError("config root must be a mapping")
    return value


def parse_yaml_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    is_list = lines[index][1].startswith("- ")
    if is_list:
        items: list[Any] = []
        while index < len(lines):
            line_indent, text = lines[index]
            if line_indent != indent or not text.startswith("- "):
                break
            items.append(parse_scalar(text[2:].strip()))
            index += 1
        return items, index

    mapping: dict[str, Any] = {}
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent != indent or text.startswith("- "):
            break
        key, sep, rest = text.partition(":")
        if not sep:
            raise ValueError(f"invalid config line: {text}")
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest:
            mapping[key] = parse_scalar(rest)
        else:
            if index < len(lines) and lines[index][0] > line_indent:
                mapping[key], index = parse_yaml_block(lines, index, lines[index][0])
            else:
                mapping[key] = {}
    return mapping, index


def parse_scalar(value: str) -> Any:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.lower() in {"null", "none", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def expand_env(text: str) -> str:
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        return os.environ.get(name, default or "")

    return pattern.sub(replace, text)


def normalize_path(path: str) -> str:
    p = path.replace("\\", "/")
    p = re.sub(r"/+", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p.lower()


def is_under(path: str, root: str) -> bool:
    path_n = normalize_path(path)
    root_n = normalize_path(root)
    return path_n == root_n or path_n.startswith(root_n.rstrip("/") + "/")


def api_get(
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    label: str = "API request",
) -> Any:
    if params:
        query = urllib.parse.urlencode(params)
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{query}"
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise RuntimeError(f"{label} failed: HTTP 401 Unauthorized at {url}. Check the API key in config.yml.") from exc
        raise RuntimeError(f"{label} failed: HTTP {exc.code} {exc.reason} at {url}. {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{label} failed: could not reach {url}. {describe_url_error(exc)}") from exc


def api_post_form(
    url: str,
    data: dict[str, Any],
    opener: urllib.request.OpenerDirector | None = None,
    label: str = "API request",
) -> str:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST")
    open_func = opener.open if opener else urllib.request.urlopen
    try:
        with open_func(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise RuntimeError(f"{label} failed: HTTP 401 Unauthorized at {url}. Check the username/password or API key in config.yml.") from exc
        raise RuntimeError(f"{label} failed: HTTP {exc.code} {exc.reason} at {url}. {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{label} failed: could not reach {url}. {describe_url_error(exc)}") from exc


def describe_url_error(exc: urllib.error.URLError) -> str:
    reason = exc.reason
    if isinstance(reason, socket.gaierror):
        return (
            f"DNS/name lookup failed for the host. This usually means the mediacleanup container is not on the "
            f"same Docker network as the media app, or config.yml uses the wrong service/container name. Details: {reason}"
        )
    if isinstance(reason, TimeoutError):
        return "Connection timed out. Check the service URL, port, and Docker network."
    return str(reason)


def fetch_radarr(config: dict[str, Any]) -> dict[str, Any]:
    radarr = config.get("radarr", {})
    if not radarr.get("enabled", True):
        return {"movies": [], "movie_files": {}}
    url = str(radarr["url"]).rstrip("/")
    headers = {"X-Api-Key": str(radarr["api_key"])}
    log("Reading Radarr library...")
    movies = api_get(f"{url}/api/v3/movie", headers=headers, label="Radarr movie library")
    movie_files: dict[int, Any] = {}
    for movie in movies:
        movie_file = movie.get("movieFile")
        if movie_file and movie_file.get("id"):
            movie_files[int(movie_file["id"])] = movie_file
    return {"movies": movies, "movie_files": movie_files}


def fetch_sonarr(config: dict[str, Any]) -> dict[str, Any]:
    sonarr = config.get("sonarr", {})
    if not sonarr.get("enabled", True):
        return {"series": [], "episodes": [], "episode_files": {}}
    url = str(sonarr["url"]).rstrip("/")
    headers = {"X-Api-Key": str(sonarr["api_key"])}
    log("Reading Sonarr series and episodes...")
    series = api_get(f"{url}/api/v3/series", headers=headers, label="Sonarr series library")
    episodes: list[dict[str, Any]] = []
    episode_files: dict[int, Any] = {}
    for item in series:
        series_id = item.get("id")
        if series_id is None:
            continue
        episodes.extend(api_get(f"{url}/api/v3/episode", headers=headers, params={"seriesId": series_id}, label=f"Sonarr episodes for series {series_id}"))
        for item in api_get(
            f"{url}/api/v3/episodefile",
            headers=headers,
            params={"seriesId": series_id},
            label=f"Sonarr episode files for series {series_id}",
        ):
            episode_files[int(item["id"])] = item
    return {"series": series, "episodes": episodes, "episode_files": episode_files}


def chunks(values: list[Any], size: int) -> list[list[Any]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def fetch_jellyfin_paths(config: dict[str, Any]) -> set[str]:
    jellyfin = config.get("jellyfin", {})
    if not jellyfin.get("enabled", True):
        return set()
    url = str(jellyfin["url"]).rstrip("/")
    headers = {"X-Emby-Token": str(jellyfin["api_key"])}
    log("Reading Jellyfin visible media paths...")
    user_id = str(jellyfin.get("user_id") or "").strip()
    if not user_id:
        user_id = fetch_jellyfin_user_id(url, headers)
    paths: set[str] = set()
    start = 0
    limit = 1000
    while True:
        payload = api_get(
            f"{url}/Users/{urllib.parse.quote(user_id)}/Items",
            headers=headers,
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "Fields": "Path",
                "StartIndex": start,
                "Limit": limit,
            },
            label="Jellyfin visible items",
        )
        items = payload.get("Items", [])
        for item in items:
            item_path = item.get("Path")
            if item_path:
                paths.add(normalize_path(item_path))
        total = payload.get("TotalRecordCount", 0)
        start += len(items)
        if start >= total or not items:
            break
    return paths


def fetch_jellyfin_user_id(url: str, headers: dict[str, str]) -> str:
    users = api_get(f"{url}/Users", headers=headers, label="Jellyfin users")
    for user in users:
        policy = user.get("Policy") or {}
        if not policy.get("IsDisabled") and user.get("Id"):
            return str(user["Id"])
    raise RuntimeError("Jellyfin users lookup returned no enabled users. Set jellyfin.user_id in config.yml.")


def fetch_qbit_paths(config: dict[str, Any]) -> set[str]:
    qbit = config.get("qbittorrent", {})
    if not qbit.get("enabled", True):
        return set()
    url = str(qbit["url"]).rstrip("/")
    log("Reading qBittorrent active paths...")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    login_text = api_post_form(
        f"{url}/api/v2/auth/login",
        {"username": qbit.get("username", ""), "password": qbit.get("password", "")},
        opener,
        label="qBittorrent login",
    )
    if login_text.strip().lower() not in {"ok.", "ok"}:
        raise RuntimeError("qBittorrent login failed")
    with opener.open(f"{url}/api/v2/torrents/info", timeout=60) as response:
        torrents = json.loads(response.read().decode("utf-8"))
    paths: set[str] = set()
    for torrent in torrents:
        content_path = torrent.get("content_path")
        save_path = torrent.get("save_path")
        if content_path:
            paths.add(normalize_path(content_path))
        elif save_path and torrent.get("name"):
            paths.add(normalize_path(posixpath.join(str(save_path), str(torrent["name"]))))
        elif save_path:
            paths.add(normalize_path(save_path))
    return paths


def scan_video_files(config: dict[str, Any]) -> list[VideoFile]:
    scan = config.get("scan", {})
    roots = scan.get("roots") or [
        config.get("media_roots", {}).get("movies"),
        config.get("media_roots", {}).get("tv"),
        config.get("media_roots", {}).get("downloads"),
    ]
    roots = [str(r) for r in roots if r]
    extensions = {str(ext).lower() for ext in scan.get("video_extensions", DEFAULT_VIDEO_EXTENSIONS)}
    ignored = [str(keyword).lower() for keyword in scan.get("ignore_path_keywords", [])]
    found: list[VideoFile] = []
    log("Scanning configured roots for video files...")
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            log(f"Warning: scan root does not exist: {root}")
            continue
        for path in root_path.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in extensions:
                continue
            path_text = path.as_posix()
            lowered = path_text.lower()
            if any(keyword in lowered for keyword in ignored):
                continue
            stat = path.stat()
            found.append(
                VideoFile(
                    path=path_text,
                    norm_path=normalize_path(path_text),
                    size=stat.st_size,
                    device=getattr(stat, "st_dev", None),
                    inode=getattr(stat, "st_ino", None),
                    nlink=getattr(stat, "st_nlink", None),
                    source_root=root,
                )
            )
    return found


def annotate_files(files: list[VideoFile], jellyfin_paths: set[str], qbit_paths: set[str]) -> list[VideoFile]:
    annotated: list[VideoFile] = []
    for vf in files:
        protected = any(paths_overlap(vf.norm_path, qbit_path) for qbit_path in qbit_paths)
        visible = vf.norm_path in jellyfin_paths
        annotated.append(dataclasses.replace(vf, protected_by_qbit=protected, jellyfin_visible=visible))
    return annotated


def paths_overlap(left: str, right: str) -> bool:
    left_n = normalize_path(left)
    right_n = normalize_path(right)
    return left_n == right_n or left_n.startswith(right_n.rstrip("/") + "/") or right_n.startswith(left_n.rstrip("/") + "/")


def build_groups(
    config: dict[str, Any],
    files: list[VideoFile],
    radarr_data: dict[str, Any],
    sonarr_data: dict[str, Any],
) -> tuple[list[MediaGroup], list[VideoFile]]:
    groups: list[MediaGroup] = []
    matched_paths: set[str] = set()

    for movie in radarr_data.get("movies", []):
        movie_id = movie.get("id")
        title = movie.get("title") or movie.get("originalTitle") or f"Movie {movie_id}"
        expected = movie.get("path", "")
        candidates = gather_movie_candidates(files, expected)
        movie_file = movie.get("movieFile") or {}
        movie_path = resolve_media_file_path(expected, movie_file)
        if movie_path:
            candidates.extend(match_exact_or_same_dir(files, movie_path))
        candidates = expand_by_hardlink(files, unique_files(candidates))
        if candidates:
            matched_paths.update(v.norm_path for v in candidates)
            groups.append(MediaGroup(f"radarr:{movie_id}", "movie", title, expected, str(movie_id), candidates))

    series_by_id = {s.get("id"): s for s in sonarr_data.get("series", [])}
    episodes_by_file: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for episode in sonarr_data.get("episodes", []):
        file_id = episode.get("episodeFileId")
        if file_id:
            episodes_by_file[int(file_id)].append(episode)

    for file_id, episode_file in sonarr_data.get("episode_files", {}).items():
        episodes = episodes_by_file.get(int(file_id), [])
        first = episodes[0] if episodes else {}
        series = series_by_id.get(first.get("seriesId"), {})
        title = format_episode_title(series, episodes, file_id)
        series_path = series.get("path", "")
        episode_path = resolve_media_file_path(series_path, episode_file)
        candidates = gather_episode_candidates(files, episode_path, series_path, episodes)
        candidates = expand_by_hardlink(files, unique_files(candidates))
        if candidates:
            matched_paths.update(v.norm_path for v in candidates)
            episode_id = first.get("id", file_id)
            groups.append(MediaGroup(f"sonarr:{file_id}", "episode", title, series_path, str(episode_id), candidates))

    unmatched = [vf for vf in files if vf.norm_path not in matched_paths]
    return groups, unmatched


def resolve_media_file_path(base_path: str, media_file: dict[str, Any]) -> str:
    direct_path = media_file.get("path")
    if direct_path:
        return str(direct_path)
    relative_path = media_file.get("relativePath")
    if base_path and relative_path:
        return posixpath.join(str(base_path).replace("\\", "/"), str(relative_path).replace("\\", "/"))
    return ""


def gather_movie_candidates(files: list[VideoFile], expected_path: str) -> list[VideoFile]:
    if not expected_path:
        return []
    expected_norm = normalize_path(expected_path)
    if expected_norm.endswith(tuple(DEFAULT_VIDEO_EXTENSIONS)):
        parent = normalize_path(posixpath.dirname(expected_path.replace("\\", "/")))
    else:
        parent = expected_norm
    return [vf for vf in files if vf.norm_path == expected_norm or vf.norm_path.startswith(parent.rstrip("/") + "/")]


def gather_episode_candidates(
    files: list[VideoFile],
    episode_path: str,
    series_path: str,
    episodes: list[dict[str, Any]],
) -> list[VideoFile]:
    if not episode_path:
        return []
    exact = normalize_path(episode_path)
    tokens = episode_tokens(episodes)
    if not tokens:
        return [vf for vf in files if vf.norm_path == exact]
    series_norm = normalize_path(series_path) if series_path else ""
    matches: list[VideoFile] = []
    for vf in files:
        if vf.norm_path == exact:
            matches.append(vf)
            continue
        if series_norm and not is_under(vf.path, series_norm):
            continue
        name = Path(vf.path).name.lower()
        if any(token in name for token in tokens):
            matches.append(vf)
    return matches


def episode_tokens(episodes: list[dict[str, Any]]) -> list[str]:
    tokens: list[str] = []
    for episode in episodes:
        season = episode.get("seasonNumber")
        number = episode.get("episodeNumber")
        if season is None or number is None:
            continue
        tokens.append(f"s{int(season):02d}e{int(number):02d}")
        tokens.append(f"{int(season)}x{int(number):02d}")
    return tokens


def match_exact_or_same_dir(files: list[VideoFile], path: str) -> list[VideoFile]:
    if not path:
        return []
    path_norm = normalize_path(path)
    parent = normalize_path(posixpath.dirname(path.replace("\\", "/")))
    return [vf for vf in files if vf.norm_path == path_norm or normalize_path(posixpath.dirname(vf.path)) == parent]


def unique_files(files: list[VideoFile]) -> list[VideoFile]:
    seen: set[str] = set()
    unique: list[VideoFile] = []
    for vf in files:
        if vf.norm_path in seen:
            continue
        seen.add(vf.norm_path)
        unique.append(vf)
    return unique


def expand_by_hardlink(all_files: list[VideoFile], candidates: list[VideoFile]) -> list[VideoFile]:
    keys = {vf.hardlink_key for vf in candidates if vf.hardlink_key}
    if not keys:
        return candidates
    expanded = list(candidates)
    seen = {vf.norm_path for vf in expanded}
    for vf in all_files:
        if vf.norm_path not in seen and vf.hardlink_key in keys:
            expanded.append(vf)
            seen.add(vf.norm_path)
    return expanded


def format_episode_title(series: dict[str, Any], episodes: list[dict[str, Any]], file_id: int) -> str:
    series_title = series.get("title") or f"Series file {file_id}"
    if not episodes:
        return series_title
    parts = []
    for ep in sorted(episodes, key=lambda e: (e.get("seasonNumber", 0), e.get("episodeNumber", 0))):
        parts.append(f"S{int(ep.get('seasonNumber', 0)):02d}E{int(ep.get('episodeNumber', 0)):02d}")
    return f"{series_title} {'/'.join(parts)}"


def classify_groups(config: dict[str, Any], groups: list[MediaGroup]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summary_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    movies_root = config.get("media_roots", {}).get("movies", "")
    tv_root = config.get("media_roots", {}).get("tv", "")

    for group in groups:
        if len(group.files) < 2:
            continue
        files = sorted(group.files, key=lambda f: (f.size, f.path))
        keeper = files[0]
        root = movies_root if group.kind == "movie" else tv_root
        keeper_in_library = bool(root and is_under(keeper.path, root))
        keeper_authoritative = keeper.jellyfin_visible or is_expected_import_path(group, keeper)
        candidates: list[VideoFile] = []
        review: list[VideoFile] = []
        for vf in files[1:]:
            same_hardlink = keeper.hardlink_key is not None and keeper.hardlink_key == vf.hardlink_key
            only_visible = vf.jellyfin_visible and not any(other.jellyfin_visible for other in files if other.norm_path != vf.norm_path)
            safe = (
                vf.size > keeper.size
                and not same_hardlink
                and keeper_in_library
                and keeper_authoritative
                and not vf.protected_by_qbit
                and not only_visible
            )
            if safe:
                candidates.append(vf)
            else:
                review.append(vf)
            detail_rows.append(make_detail_row(group, vf, keeper, safe, same_hardlink, only_visible))

        if candidates or review:
            reclaimable = sum(vf.size for vf in candidates)
            summary_rows.append(
                {
                    "kind": group.kind,
                    "title": group.title,
                    "item_id": group.item_id,
                    "file_count": len(files),
                    "keeper": keeper.path,
                    "keeper_size": keeper.size,
                    "safe_cleanup_count": len(candidates),
                    "review_count": len(review),
                    "reclaimable_bytes": reclaimable,
                    "reclaimable_human": human_size(reclaimable),
                    "status": "safe_cleanup_candidate" if candidates else "review",
                }
            )
    return summary_rows, detail_rows


def is_expected_import_path(group: MediaGroup, vf: VideoFile) -> bool:
    return vf.norm_path.startswith(normalize_path(group.expected_path).rstrip("/") + "/") or vf.norm_path == normalize_path(group.expected_path)


def make_detail_row(
    group: MediaGroup,
    vf: VideoFile,
    keeper: VideoFile,
    safe: bool,
    same_hardlink: bool,
    only_visible: bool,
) -> dict[str, Any]:
    reasons = []
    if same_hardlink:
        reasons.append("same hardlink as keeper")
    if vf.protected_by_qbit:
        reasons.append("protected by qBittorrent")
    if only_visible:
        reasons.append("only Jellyfin-visible version")
    if not keeper.jellyfin_visible and not is_expected_import_path(group, keeper):
        reasons.append("keeper not confirmed visible/imported")
    if vf.size <= keeper.size:
        reasons.append("not larger than keeper")
    return {
        "kind": group.kind,
        "title": group.title,
        "item_id": group.item_id,
        "path": vf.path,
        "size": vf.size,
        "size_human": human_size(vf.size),
        "keeper": keeper.path,
        "keeper_size": keeper.size,
        "keeper_size_human": human_size(keeper.size),
        "hardlink_key": vf.hardlink_key or "",
        "protected_by_qbit": vf.protected_by_qbit,
        "jellyfin_visible": vf.jellyfin_visible,
        "recommendation": "safe_cleanup_candidate" if safe else "review",
        "reason": "; ".join(reasons) if reasons else "larger confirmed duplicate",
    }


def unmatched_rows(unmatched: list[VideoFile]) -> list[dict[str, Any]]:
    rows = []
    for vf in unmatched:
        rows.append(
            {
                "kind": "unmatched",
                "title": guess_title(vf.path),
                "item_id": "",
                "path": vf.path,
                "size": vf.size,
                "size_human": human_size(vf.size),
                "keeper": "",
                "keeper_size": "",
                "keeper_size_human": "",
                "hardlink_key": vf.hardlink_key or "",
                "protected_by_qbit": vf.protected_by_qbit,
                "jellyfin_visible": vf.jellyfin_visible,
                "recommendation": "review",
                "reason": "not confirmed by Sonarr/Radarr/Jellyfin; filename guess only",
            }
        )
    return rows


def guess_title(path: str) -> str:
    name = Path(path).stem
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\b(720p|1080p|2160p|x264|x265|h264|h265|bluray|web-dl|webrip|hdrip)\b", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip()


def human_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if rows:
        fields = list(rows[0].keys())
    else:
        fields = ["status"]
        rows = [{"status": "no rows"}]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_html(path: Path, summary_rows: list[dict[str, Any]], detail_rows: list[dict[str, Any]]) -> None:
    safe_bytes = sum(int(row.get("reclaimable_bytes", 0) or 0) for row in summary_rows)
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>Media Cleanup Audit</title>",
        "<style>body{font-family:Arial,sans-serif;margin:24px;color:#222}table{border-collapse:collapse;width:100%;margin:16px 0}th,td{border:1px solid #ddd;padding:6px 8px;font-size:13px;vertical-align:top}th{background:#f4f4f4;text-align:left}.safe{color:#0a6b2b;font-weight:bold}.review{color:#9a5b00;font-weight:bold}.muted{color:#666}</style>",
        "</head><body>",
        "<h1>Media Cleanup Audit</h1>",
        f"<p><strong>Potential reclaimable space:</strong> {html.escape(human_size(safe_bytes))}</p>",
        f"<p class='muted'>Generated {html.escape(datetime.now().isoformat(timespec='seconds'))}</p>",
        "<h2>Summary</h2>",
        table_html(summary_rows),
        "<h2>Details</h2>",
        table_html(detail_rows),
        "</body></html>",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")


def run_audit(config_path: str, output_dir: str | Path) -> AuditResult:
    config = load_config(config_path)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    radarr = fetch_radarr(config)
    sonarr = fetch_sonarr(config)
    jellyfin_paths = fetch_jellyfin_paths(config)
    qbit_paths = fetch_qbit_paths(config)
    files = annotate_files(scan_video_files(config), jellyfin_paths, qbit_paths)
    groups, unmatched = build_groups(config, files, radarr, sonarr)
    summary_rows, detail_rows = classify_groups(config, groups)
    detail_rows.extend(unmatched_rows(unmatched))

    summary_csv = output_path / f"media-cleanup-summary-{stamp}.csv"
    details_csv = output_path / f"media-cleanup-details-{stamp}.csv"
    html_report = output_path / f"media-cleanup-report-{stamp}.html"
    raw_json = output_path / f"media-cleanup-raw-{stamp}.json"

    write_csv(summary_csv, summary_rows)
    write_csv(details_csv, detail_rows)
    write_html(html_report, summary_rows, detail_rows)
    raw = {
        "generated_at": datetime.now().isoformat(),
        "summary": summary_rows,
        "details": detail_rows,
        "counts": {
            "files_scanned": len(files),
            "groups": len(groups),
            "unmatched": len(unmatched),
        },
    }
    raw_json.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return AuditResult(
        stamp=stamp,
        output_dir=output_path,
        summary_rows=summary_rows,
        detail_rows=detail_rows,
        files_scanned=len(files),
        groups_count=len(groups),
        unmatched_count=len(unmatched),
        summary_csv=summary_csv,
        details_csv=details_csv,
        html_report=html_report,
        raw_json=raw_json,
    )


def table_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No rows.</p>"
    fields = list(rows[0].keys())
    out = ["<table><thead><tr>"]
    out.extend(f"<th>{html.escape(field)}</th>" for field in fields)
    out.append("</tr></thead><tbody>")
    for row in rows:
        cls = "safe" if row.get("recommendation") == "safe_cleanup_candidate" or row.get("status") == "safe_cleanup_candidate" else "review"
        out.append(f"<tr class='{cls}'>")
        for field in fields:
            out.append(f"<td>{html.escape(str(row.get(field, '')))}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def serve_dashboard(config_path: str, output_dir: str | Path, host: str, port: int) -> None:
    state = DashboardState(config_path=config_path, output_dir=Path(output_dir))
    state.output_dir.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.send_html(render_dashboard(state))
                return
            if parsed.path == "/status":
                self.send_json(render_status(state))
                return
            if parsed.path.startswith("/reports/"):
                self.send_report_file(parsed.path.removeprefix("/reports/"))
                return
            self.send_error(404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/run":
                started = start_dashboard_audit(state)
                self.send_json({"started": started, **render_status(state)})
                return
            self.send_error(404)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_html(self, body: str, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def send_report_file(self, name: str) -> None:
            safe_name = Path(urllib.parse.unquote(name)).name
            path = state.output_dir / safe_name
            if not path.exists() or not path.is_file():
                self.send_error(404)
                return
            content = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            if path.suffix.lower() in {".csv", ".json"}:
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer((host, port), Handler)
    log(f"Media Cleanup dashboard listening on http://{host}:{port}")
    server.serve_forever()


def start_dashboard_audit(state: DashboardState) -> bool:
    if state.running:
        return False

    def worker() -> None:
        state.running = True
        state.last_started = datetime.now().isoformat(timespec="seconds")
        state.last_error = ""
        try:
            result = run_audit(state.config_path, state.output_dir)
            state.last_result = result
            state.last_finished = datetime.now().isoformat(timespec="seconds")
        except Exception as exc:
            state.last_error = str(exc)
            state.last_finished = datetime.now().isoformat(timespec="seconds")
        finally:
            state.running = False

    threading.Thread(target=worker, daemon=True).start()
    return True


def render_status(state: DashboardState) -> dict[str, Any]:
    result = state.last_result
    return {
        "running": state.running,
        "last_started": state.last_started,
        "last_finished": state.last_finished,
        "last_error": state.last_error,
        "latest": result_payload(result) if result else None,
    }


def result_payload(result: AuditResult) -> dict[str, Any]:
    safe_bytes = sum(int(row.get("reclaimable_bytes", 0) or 0) for row in result.summary_rows)
    safe_count = sum(int(row.get("safe_cleanup_count", 0) or 0) for row in result.summary_rows)
    review_count = sum(int(row.get("review_count", 0) or 0) for row in result.summary_rows)
    return {
        "stamp": result.stamp,
        "files_scanned": result.files_scanned,
        "groups_count": result.groups_count,
        "unmatched_count": result.unmatched_count,
        "safe_count": safe_count,
        "review_count": review_count,
        "reclaimable": human_size(safe_bytes),
        "html_report": result.html_report.name,
        "summary_csv": result.summary_csv.name,
        "details_csv": result.details_csv.name,
        "raw_json": result.raw_json.name,
    }


def render_dashboard(state: DashboardState) -> str:
    latest = result_payload(state.last_result) if state.last_result else None
    latest_cards = render_latest_cards(latest)
    report_links = render_report_links(latest)
    error = f"<div class='notice error'>{html.escape(state.last_error)}</div>" if state.last_error else ""
    running = "true" if state.running else "false"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Media Cleanup</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #657080;
      --line: #dfe4ea;
      --green: #136f3a;
      --amber: #9a6400;
      --blue: #1459a8;
      --red: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 24px; }}
    h1 {{ font-size: 28px; margin: 0; letter-spacing: 0; }}
    .sub {{ color: var(--muted); margin-top: 6px; font-size: 14px; }}
    .button {{ border: 0; border-radius: 8px; padding: 12px 18px; background: var(--blue); color: white; font-size: 15px; font-weight: 700; cursor: pointer; min-width: 132px; }}
    .button[disabled] {{ opacity: .55; cursor: wait; }}
    .grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .metric, .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .metric {{ padding: 16px; min-height: 94px; }}
    .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 800; letter-spacing: .04em; }}
    .value {{ margin-top: 8px; font-size: 24px; font-weight: 800; overflow-wrap: anywhere; }}
    .value.green {{ color: var(--green); }}
    .value.amber {{ color: var(--amber); }}
    .panel {{ padding: 18px; margin-top: 14px; }}
    .row {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }}
    .status {{ display: inline-flex; align-items: center; gap: 8px; font-weight: 800; }}
    .dot {{ width: 10px; height: 10px; border-radius: 999px; background: var(--green); }}
    .dot.busy {{ background: var(--amber); animation: pulse 1s infinite; }}
    @keyframes pulse {{ 0%, 100% {{ opacity: .35 }} 50% {{ opacity: 1 }} }}
    .links {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }}
    .link {{ color: var(--blue); text-decoration: none; font-weight: 700; border: 1px solid var(--line); border-radius: 8px; padding: 9px 11px; background: #fff; }}
    .notice {{ padding: 12px 14px; border-radius: 8px; background: #fff7ed; border: 1px solid #fed7aa; margin-top: 14px; color: #7c2d12; }}
    .error {{ background: #fef3f2; border-color: #fecdca; color: var(--red); }}
    iframe {{ width: 100%; height: 680px; border: 1px solid var(--line); border-radius: 8px; background: white; }}
    @media (max-width: 900px) {{ main {{ padding: 18px; }} header {{ align-items: flex-start; flex-direction: column; }} .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    @media (max-width: 560px) {{ .grid {{ grid-template-columns: 1fr; }} .button {{ width: 100%; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Media Cleanup</h1>
        <div class="sub">Read-only audit for Jellyfin, Sonarr, Radarr, qBittorrent, and NAS video files.</div>
      </div>
      <button id="run" class="button" onclick="runAudit()">Run Audit</button>
    </header>
    <section id="metrics" class="grid">{latest_cards}</section>
    <section class="panel">
      <div class="row">
        <div>
          <div class="status"><span id="dot" class="dot {'busy' if state.running else ''}"></span><span id="statusText">{'Running audit' if state.running else 'Ready'}</span></div>
          <div class="sub" id="timeText">{html.escape(render_time_text(state))}</div>
        </div>
        <div class="sub">Config: {html.escape(state.config_path)} | Reports: {html.escape(str(state.output_dir))}</div>
      </div>
      <div id="links">{report_links}</div>
      <div id="error">{error}</div>
    </section>
    <section class="panel">
      <div class="row"><h2 style="margin:0;font-size:18px;">Latest Report</h2></div>
      <div style="margin-top:14px;" id="reportFrame">{render_report_frame(latest)}</div>
    </section>
  </main>
  <script>
    let running = {running};
    async function runAudit() {{
      const button = document.getElementById('run');
      button.disabled = true;
      document.getElementById('statusText').textContent = 'Starting audit';
      document.getElementById('dot').classList.add('busy');
      await fetch('/run', {{ method: 'POST' }});
      poll();
    }}
    async function poll() {{
      const res = await fetch('/status');
      const data = await res.json();
      render(data);
      if (data.running) setTimeout(poll, 1500);
    }}
    function render(data) {{
      document.getElementById('run').disabled = data.running;
      document.getElementById('dot').classList.toggle('busy', data.running);
      document.getElementById('statusText').textContent = data.running ? 'Running audit' : 'Ready';
      document.getElementById('timeText').textContent = data.last_finished ? `Last finished ${{data.last_finished}}` : (data.last_started ? `Started ${{data.last_started}}` : 'No audit has run yet');
      document.getElementById('error').innerHTML = data.last_error ? `<div class="notice error">${{escapeHtml(data.last_error)}}</div>` : '';
      if (data.latest) {{
        document.getElementById('metrics').innerHTML = cards(data.latest);
        document.getElementById('links').innerHTML = links(data.latest);
        document.getElementById('reportFrame').innerHTML = `<iframe src="/reports/${{encodeURIComponent(data.latest.html_report)}}"></iframe>`;
      }}
    }}
    function cards(x) {{
      return `
        <div class="metric"><div class="label">Scanned</div><div class="value">${{x.files_scanned}}</div></div>
        <div class="metric"><div class="label">Matched Groups</div><div class="value">${{x.groups_count}}</div></div>
        <div class="metric"><div class="label">Safe Candidates</div><div class="value green">${{x.safe_count}}</div></div>
        <div class="metric"><div class="label">Review Items</div><div class="value amber">${{x.review_count}}</div></div>
        <div class="metric"><div class="label">Reclaimable</div><div class="value green">${{x.reclaimable}}</div></div>`;
    }}
    function links(x) {{
      return `<div class="links">
        <a class="link" href="/reports/${{encodeURIComponent(x.html_report)}}" target="_blank">Open HTML</a>
        <a class="link" href="/reports/${{encodeURIComponent(x.summary_csv)}}">Summary CSV</a>
        <a class="link" href="/reports/${{encodeURIComponent(x.details_csv)}}">Details CSV</a>
        <a class="link" href="/reports/${{encodeURIComponent(x.raw_json)}}">Raw JSON</a>
      </div>`;
    }}
    function escapeHtml(s) {{
      return s.replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    if (running) poll();
  </script>
</body>
</html>"""


def render_latest_cards(latest: dict[str, Any] | None) -> str:
    if not latest:
        return """
        <div class="metric"><div class="label">Scanned</div><div class="value">0</div></div>
        <div class="metric"><div class="label">Matched Groups</div><div class="value">0</div></div>
        <div class="metric"><div class="label">Safe Candidates</div><div class="value green">0</div></div>
        <div class="metric"><div class="label">Review Items</div><div class="value amber">0</div></div>
        <div class="metric"><div class="label">Reclaimable</div><div class="value green">0 B</div></div>"""
    return f"""
        <div class="metric"><div class="label">Scanned</div><div class="value">{latest['files_scanned']}</div></div>
        <div class="metric"><div class="label">Matched Groups</div><div class="value">{latest['groups_count']}</div></div>
        <div class="metric"><div class="label">Safe Candidates</div><div class="value green">{latest['safe_count']}</div></div>
        <div class="metric"><div class="label">Review Items</div><div class="value amber">{latest['review_count']}</div></div>
        <div class="metric"><div class="label">Reclaimable</div><div class="value green">{html.escape(str(latest['reclaimable']))}</div></div>"""


def render_report_links(latest: dict[str, Any] | None) -> str:
    if not latest:
        return "<div class='notice'>No audit has run yet. Press Run Audit to generate the first report.</div>"
    return f"""<div class="links">
      <a class="link" href="/reports/{urllib.parse.quote(str(latest['html_report']))}" target="_blank">Open HTML</a>
      <a class="link" href="/reports/{urllib.parse.quote(str(latest['summary_csv']))}">Summary CSV</a>
      <a class="link" href="/reports/{urllib.parse.quote(str(latest['details_csv']))}">Details CSV</a>
      <a class="link" href="/reports/{urllib.parse.quote(str(latest['raw_json']))}">Raw JSON</a>
    </div>"""


def render_report_frame(latest: dict[str, Any] | None) -> str:
    if not latest:
        return "<div class='notice'>The latest HTML report will appear here after the first audit.</div>"
    return f'<iframe src="/reports/{urllib.parse.quote(str(latest["html_report"]))}"></iframe>'


def render_time_text(state: DashboardState) -> str:
    if state.last_finished:
        return f"Last finished {state.last_finished}"
    if state.last_started:
        return f"Started {state.last_started}"
    return "No audit has run yet"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only media cleanup audit")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--serve", action="store_true", help="run the web dashboard instead of a one-shot audit")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=6996)
    args = parser.parse_args()

    try:
        if args.serve:
            load_config(args.config)
            serve_dashboard(args.config, args.output_dir, args.host, args.port)
            return 0
        result = run_audit(args.config, args.output_dir)
        log(
            f"Done. Scanned {result.files_scanned} files, built {result.groups_count} confirmed groups, "
            f"left {result.unmatched_count} files for review."
        )
        log(f"Reports written to {result.output_dir}")
        return 0
    except Exception as exc:
        log(f"Media Cleanup failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

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
import shutil
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
from typing import Any, Callable

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
    modified_at: float = 0
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
    diagnostic_rows: list[dict[str, Any]]
    files_scanned: int
    groups_count: int
    unmatched_count: int
    summary_csv: Path
    details_csv: Path
    html_report: Path
    raw_json: Path


@dataclasses.dataclass(frozen=True)
class ScanResult:
    files: list[VideoFile]
    errors: list[dict[str, str]]


@dataclasses.dataclass
class DashboardState:
    config_path: str
    output_dir: Path
    running: bool = False
    last_started: str = ""
    last_finished: str = ""
    last_error: str = ""
    last_result: AuditResult | None = None
    action_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    action_id: int = 0
    action_status: dict[str, Any] = dataclasses.field(default_factory=dict)


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


def canonicalize_path(config: dict[str, Any], path: str) -> str:
    mapped = apply_path_mappings(config, path)
    return normalize_path(mapped)


def apply_path_mappings(config: dict[str, Any], path: str) -> str:
    if not path:
        return path
    mappings = config.get("path_mappings", []) or []
    path_text = path.replace("\\", "/")
    path_norm = normalize_path(path_text)
    ordered = sorted(mappings, key=lambda m: len(str(m.get("from", ""))), reverse=True)
    for mapping in ordered:
        source = str(mapping.get("from", "")).replace("\\", "/").rstrip("/")
        target = str(mapping.get("to", "")).replace("\\", "/").rstrip("/")
        if not source or not target:
            continue
        source_norm = normalize_path(source)
        if path_norm == source_norm:
            return target
        if path_norm.startswith(source_norm + "/"):
            suffix = path_text[len(source):].lstrip("/")
            return f"{target}/{suffix}" if suffix else target
    return path_text


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


def fetch_jellyfin_paths(config: dict[str, Any]) -> tuple[set[str], list[str]]:
    jellyfin = config.get("jellyfin", {})
    if not jellyfin.get("enabled", True):
        return set(), []
    url = str(jellyfin["url"]).rstrip("/")
    headers = {"X-Emby-Token": str(jellyfin["api_key"])}
    log("Reading Jellyfin visible media paths...")
    user_id = str(jellyfin.get("user_id") or "").strip()
    if not user_id:
        user_id = fetch_jellyfin_user_id(url, headers)
    paths: set[str] = set()
    raw_paths: list[str] = []
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
                raw_paths.append(str(item_path))
                paths.add(canonicalize_path(config, item_path))
        total = payload.get("TotalRecordCount", 0)
        start += len(items)
        if start >= total or not items:
            break
    return paths, raw_paths


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
        raise RuntimeError(
            "qBittorrent login failed. Check qbittorrent.username/qbittorrent.password in config.yml "
            f"and QBIT_USER/QBIT_PASS in .env. qBittorrent returned: {login_text[:200]!r}"
        )
    with opener.open(f"{url}/api/v2/torrents/info", timeout=60) as response:
        torrents = json.loads(response.read().decode("utf-8"))
    paths: set[str] = set()
    for torrent in torrents:
        content_path = torrent.get("content_path")
        save_path = torrent.get("save_path")
        if content_path:
            paths.add(canonicalize_path(config, content_path))
        elif save_path and torrent.get("name"):
            paths.add(canonicalize_path(config, posixpath.join(str(save_path), str(torrent["name"]))))
        elif save_path:
            paths.add(canonicalize_path(config, save_path))
    return paths


def scan_video_files(config: dict[str, Any]) -> ScanResult:
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
    errors: list[dict[str, str]] = []
    log("Scanning configured roots for video files...")
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            log(f"Warning: scan root does not exist: {root}")
            continue
        for path in safe_rglob(root_path, errors):
            try:
                if not path.is_file():
                    continue
            except OSError as exc:
                errors.append({"path": path.as_posix(), "reason": f"cannot inspect path: {exc}"})
                continue
            if path.suffix.lower() not in extensions:
                continue
            path_text = path.as_posix()
            lowered = path_text.lower()
            if any(keyword in lowered for keyword in ignored):
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                errors.append({"path": path_text, "reason": f"cannot stat video file: {exc}"})
                continue
            found.append(
                VideoFile(
                    path=path_text,
                    norm_path=canonicalize_path(config, path_text),
                    size=stat.st_size,
                    device=getattr(stat, "st_dev", None),
                    inode=getattr(stat, "st_ino", None),
                    nlink=getattr(stat, "st_nlink", None),
                    source_root=root,
                    modified_at=getattr(stat, "st_mtime", 0),
                )
            )
    return ScanResult(files=found, errors=errors)


def safe_rglob(root: Path, errors: list[dict[str, str]]) -> list[Path]:
    found: list[Path] = []
    try:
        iterator = root.rglob("*")
        for path in iterator:
            found.append(path)
    except OSError as exc:
        errors.append({"path": root.as_posix(), "reason": f"cannot scan directory: {exc}"})
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
        expected = apply_path_mappings(config, str(movie.get("path", "")))
        candidates = gather_movie_candidates(files, expected)
        movie_file = movie.get("movieFile") or {}
        movie_path = apply_path_mappings(config, resolve_media_file_path(str(movie.get("path", "")), movie_file))
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
        raw_series_path = str(series.get("path", ""))
        series_path = apply_path_mappings(config, raw_series_path)
        episode_path = apply_path_mappings(config, resolve_media_file_path(raw_series_path, episode_file))
        candidates = gather_episode_candidates(files, episode_path, series_path, episodes)
        candidates = expand_by_hardlink(files, unique_files(candidates))
        if candidates:
            matched_paths.update(v.norm_path for v in candidates)
            episode_id = first.get("id", file_id)
            groups.append(MediaGroup(f"sonarr:{file_id}", "episode", title, series_path, str(episode_id), candidates))

    unmatched = [vf for vf in files if vf.norm_path not in matched_paths]
    return groups, unmatched


def library_index_rows(groups: list[MediaGroup]) -> list[dict[str, Any]]:
    rows = []
    for group in groups:
        if not group.expected_path:
            continue
        expected_files = [vf for vf in group.files if is_under(vf.path, group.expected_path)]
        if not expected_files:
            continue
        keeper = min(expected_files, key=lambda vf: (vf.size, vf.path))
        parsed = parse_media_identity(keeper.path)
        rows.append(
            {
                "kind": group.kind,
                "title": group.title,
                "identity": parsed.get("parsed_id", "") or group.title,
                "path": keeper.path,
                "size": keeper.size,
                "size_human": human_size(keeper.size),
                "jellyfin_visible": keeper.jellyfin_visible,
            }
        )
    return rows


def diagnostic_rows(
    config: dict[str, Any],
    files: list[VideoFile],
    radarr_data: dict[str, Any],
    sonarr_data: dict[str, Any],
    jellyfin_raw_paths: list[str],
    jellyfin_paths: set[str],
    qbit_paths: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for vf in files[:10]:
        rows.append({"source": "filesystem", "raw_path": vf.path, "mapped_path": vf.norm_path})
    for movie in radarr_data.get("movies", [])[:10]:
        raw = str(movie.get("path", ""))
        movie_file = movie.get("movieFile") or {}
        raw_file = resolve_media_file_path(raw, movie_file)
        rows.append({"source": "radarr_movie", "raw_path": raw, "mapped_path": canonicalize_path(config, raw)})
        if raw_file:
            rows.append({"source": "radarr_movie_file", "raw_path": raw_file, "mapped_path": canonicalize_path(config, raw_file)})
    for series in sonarr_data.get("series", [])[:10]:
        raw = str(series.get("path", ""))
        rows.append({"source": "sonarr_series", "raw_path": raw, "mapped_path": canonicalize_path(config, raw)})
    for episode_file in list(sonarr_data.get("episode_files", {}).values())[:10]:
        raw = str(episode_file.get("path") or episode_file.get("relativePath") or "")
        rows.append({"source": "sonarr_episode_file", "raw_path": raw, "mapped_path": canonicalize_path(config, raw)})
    for raw in jellyfin_raw_paths[:10]:
        rows.append({"source": "jellyfin", "raw_path": raw, "mapped_path": canonicalize_path(config, raw)})
    for mapped in sorted(jellyfin_paths)[:10]:
        rows.append({"source": "jellyfin_mapped", "raw_path": "", "mapped_path": mapped})
    for mapped in sorted(qbit_paths)[:10]:
        rows.append({"source": "qbittorrent_mapped", "raw_path": "", "mapped_path": mapped})
    return rows


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


def unmatched_rows(config: dict[str, Any], unmatched: list[VideoFile]) -> list[dict[str, Any]]:
    rows = []
    for vf in unmatched:
        classification = classify_unmatched(config, vf)
        rows.append(
            {
                "kind": "unmatched",
                "title": guess_title(vf.path),
                "item_id": "",
                "path": vf.path,
                "location": classification["location"],
                "possible_type": classification["possible_type"],
                "parsed_id": classification["parsed_id"],
                "size": vf.size,
                "size_human": human_size(vf.size),
                "modified": format_timestamp(vf.modified_at),
                "age_days": age_days(vf.modified_at),
                "folder": str(Path(vf.path).parent),
                "keeper": "",
                "keeper_size": "",
                "keeper_size_human": "",
                "hardlink_key": vf.hardlink_key or "",
                "protected_by_qbit": vf.protected_by_qbit,
                "jellyfin_visible": vf.jellyfin_visible,
                "recommendation": "review",
                "reason": classification["reason"],
            }
        )
    return rows


def classify_unmatched(config: dict[str, Any], vf: VideoFile) -> dict[str, str]:
    media_roots = config.get("media_roots", {})
    path = vf.path
    location = "other"
    reason = "not confirmed by Sonarr/Radarr/Jellyfin; filename guess only"
    for name, root in {
        "movies": media_roots.get("movies", ""),
        "tv": media_roots.get("tv", ""),
        "downloads": media_roots.get("downloads", ""),
    }.items():
        if root and is_under(path, root):
            location = name
            break
    if "/data/anime" in normalize_path(path):
        location = "anime"
    if location in {"movies", "tv", "anime"}:
        reason = "unmatched inside library root; likely orphan/zombie candidate, review before cleanup"
    elif location == "downloads":
        reason = "unmatched inside downloads; may be active, incomplete, seeding, or not imported"

    parsed = parse_media_identity(path)
    return {
        "location": location,
        "possible_type": parsed["possible_type"],
        "parsed_id": parsed["parsed_id"],
        "reason": reason,
    }


def parse_media_identity(path: str) -> dict[str, str]:
    name = Path(path).stem
    tv_match = re.search(r"\bS(?P<season>\d{1,2})E(?P<episode>\d{1,3})\b", name, flags=re.I)
    if not tv_match:
        tv_match = re.search(r"\b(?P<season>\d{1,2})x(?P<episode>\d{1,3})\b", name, flags=re.I)
    if tv_match:
        title = guess_title(name[: tv_match.start()])
        parsed_id = f"{title} S{int(tv_match.group('season')):02d}E{int(tv_match.group('episode')):02d}".strip()
        return {"possible_type": "episode", "parsed_id": parsed_id}
    movie_match = re.search(r"\b(19\d{2}|20\d{2})\b", name)
    if movie_match:
        title = guess_title(name[: movie_match.start()])
        return {"possible_type": "movie", "parsed_id": f"{title} ({movie_match.group(1)})".strip()}
    return {"possible_type": "unknown", "parsed_id": guess_title(path)}


def unmatched_breakdown_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("location", "other")), str(row.get("possible_type", "unknown")))
        bucket = buckets.setdefault(
            key,
            {
                "location": key[0],
                "possible_type": key[1],
                "file_count": 0,
                "total_bytes": 0,
                "total_size": "0 B",
            },
        )
        bucket["file_count"] += 1
        try:
            bucket["total_bytes"] += int(row.get("size", 0) or 0)
        except ValueError:
            pass
    for bucket in buckets.values():
        bucket["total_size"] = human_size(int(bucket["total_bytes"]))
    return sorted(buckets.values(), key=lambda row: (str(row["location"]), str(row["possible_type"])))


def scan_breakdown_rows(config: dict[str, Any], files: list[VideoFile]) -> list[dict[str, Any]]:
    roots = config.get("media_roots", {})
    labels = [
        ("movies", roots.get("movies", "")),
        ("tv", roots.get("tv", "")),
        ("downloads", roots.get("downloads", "")),
    ]
    scan_roots = [str(root) for root in config.get("scan", {}).get("roots", []) or []]
    for root in scan_roots:
        if root and "anime" in normalize_path(root) and not any(label == "anime" for label, _ in labels):
            labels.insert(2, ("anime", root))
    buckets: dict[str, dict[str, Any]] = {}
    for label, root in labels:
        if root:
            buckets[label] = {"location": label, "root": root, "file_count": 0, "total_bytes": 0, "total_size": "0 B"}
    buckets["other"] = {"location": "other", "root": "", "file_count": 0, "total_bytes": 0, "total_size": "0 B"}
    for vf in files:
        label = "other"
        for candidate, root in labels:
            if root and is_under(vf.path, root):
                label = candidate
                break
        bucket = buckets.setdefault(label, {"location": label, "root": "", "file_count": 0, "total_bytes": 0, "total_size": "0 B"})
        bucket["file_count"] += 1
        bucket["total_bytes"] += vf.size
    for bucket in buckets.values():
        bucket["total_size"] = human_size(int(bucket["total_bytes"]))
    return [row for row in buckets.values() if row["file_count"] or row["location"] != "other"]


def scan_error_rows(errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for error in errors:
        path = error.get("path", "")
        rows.append(
            {
                "kind": "inaccessible",
                "title": guess_title(path),
                "item_id": "",
                "path": path,
                "size": "",
                "size_human": "",
                "keeper": "",
                "keeper_size": "",
                "keeper_size_human": "",
                "hardlink_key": "",
                "protected_by_qbit": "",
                "jellyfin_visible": "",
                "recommendation": "review",
                "reason": error.get("reason", "cannot inspect file"),
            }
        )
    return rows


def guess_title(path: str) -> str:
    name = Path(path).stem
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\b(720p|1080p|2160p|x264|x265|h264|h265|bluray|web-dl|webrip|hdrip)\b", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip()


def format_timestamp(value: float) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


def age_days(value: float) -> int | str:
    if not value:
        return ""
    delta = datetime.now() - datetime.fromtimestamp(value)
    return max(0, int(delta.total_seconds() // 86400))


def human_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if rows:
        fields = row_fields(rows)
    else:
        fields = ["status"]
        rows = [{"status": "no rows"}]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_html(
    path: Path,
    summary_rows: list[dict[str, Any]],
    detail_rows: list[dict[str, Any]],
    unmatched_breakdown: list[dict[str, Any]] | None = None,
    diagnostic_rows: list[dict[str, Any]] | None = None,
    counts: dict[str, int] | None = None,
) -> None:
    safe_bytes = sum(int(row.get("reclaimable_bytes", 0) or 0) for row in summary_rows)
    counts = counts or {}
    unmatched_breakdown = unmatched_breakdown or []
    diagnostic_rows = diagnostic_rows or []
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>Media Cleanup Audit</title>",
        "<style>body{font-family:Arial,sans-serif;margin:24px;color:#222}table{border-collapse:collapse;width:100%;margin:16px 0}th,td{border:1px solid #ddd;padding:6px 8px;font-size:13px;vertical-align:top}th{background:#f4f4f4;text-align:left}.safe{color:#0a6b2b;font-weight:bold}.review{color:#9a5b00;font-weight:bold}.muted{color:#666}</style>",
        "</head><body>",
        "<h1>Media Cleanup Audit</h1>",
        f"<p><strong>Potential reclaimable space:</strong> {html.escape(human_size(safe_bytes))}</p>",
        f"<p class='muted'>Generated {html.escape(datetime.now().isoformat(timespec='seconds'))}</p>",
        "<h2>Run Stats</h2>",
        table_html([{
            "files_scanned": counts.get("files_scanned", 0),
            "matched_groups": counts.get("groups", 0),
            "unmatched_files": counts.get("unmatched", 0),
            "scan_errors": counts.get("scan_errors", 0),
            "summary_rows": len(summary_rows),
            "detail_rows": len(detail_rows),
        }]),
        "<h2>Unmatched Breakdown</h2>",
        table_html(unmatched_breakdown),
        "<h2>Summary</h2>",
        table_html(summary_rows),
        "<h2>Details</h2>",
        table_html(detail_rows),
        "<h2>Path Diagnostics</h2>",
        table_html(diagnostic_rows),
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
    jellyfin_paths, jellyfin_raw_paths = fetch_jellyfin_paths(config)
    qbit_paths = fetch_qbit_paths(config)
    scan_result = scan_video_files(config)
    files = annotate_files(scan_result.files, jellyfin_paths, qbit_paths)
    groups, unmatched = build_groups(config, files, radarr, sonarr)
    library_index = library_index_rows(groups)
    summary_rows, detail_rows = classify_groups(config, groups)
    unmatched_detail_rows = unmatched_rows(config, unmatched)
    unmatched_breakdown = unmatched_breakdown_rows(unmatched_detail_rows)
    scan_breakdown = scan_breakdown_rows(config, files)
    detail_rows.extend(unmatched_detail_rows)
    detail_rows.extend(scan_error_rows(scan_result.errors))
    diagnostics = diagnostic_rows(config, files, radarr, sonarr, jellyfin_raw_paths, jellyfin_paths, qbit_paths)

    summary_csv = output_path / f"media-cleanup-summary-{stamp}.csv"
    details_csv = output_path / f"media-cleanup-details-{stamp}.csv"
    html_report = output_path / f"media-cleanup-report-{stamp}.html"
    raw_json = output_path / f"media-cleanup-raw-{stamp}.json"

    write_csv(summary_csv, summary_rows)
    write_csv(details_csv, detail_rows)
    raw = {
        "generated_at": datetime.now().isoformat(),
        "summary": summary_rows,
        "details": detail_rows,
        "unmatched_breakdown": unmatched_breakdown,
        "scan_breakdown": scan_breakdown,
        "library_index": library_index,
        "diagnostics": diagnostics,
        "counts": {
            "files_scanned": len(files),
            "groups": len(groups),
            "unmatched": len(unmatched),
            "scan_errors": len(scan_result.errors),
        },
    }
    write_html(html_report, summary_rows, detail_rows, unmatched_breakdown, diagnostics, raw["counts"])
    raw_json.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return AuditResult(
        stamp=stamp,
        output_dir=output_path,
        summary_rows=summary_rows,
        detail_rows=detail_rows,
        diagnostic_rows=diagnostics,
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
    fields = row_fields(rows)
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


def row_fields(rows: list[dict[str, Any]]) -> list[str]:
    fields = []
    seen = set()
    for row in rows:
        for field in row.keys():
            if field not in seen:
                fields.append(field)
                seen.add(field)
    return fields


def latest_raw_payload(state: DashboardState) -> dict[str, Any] | None:
    result = state.last_result
    if not result or not result.raw_json.exists():
        latest = sorted(state.output_dir.glob("media-cleanup-raw-*.json"), reverse=True)
        if not latest:
            return None
        try:
            return json.loads(latest[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    try:
        return json.loads(result.raw_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def dashboard_data(state: DashboardState) -> dict[str, Any]:
    raw = latest_raw_payload(state)
    latest = result_payload(state.last_result) if state.last_result else raw_result_payload(raw)
    details = raw.get("details", []) if raw else []
    duplicate_rows = [
        row for row in details
        if row.get("recommendation") == "safe_cleanup_candidate" and row.get("kind") in {"movie", "episode"}
    ]
    library_review_rows = [
        row for row in details
        if row.get("kind") in {"unmatched", "inaccessible"}
        and row.get("location") in {"movies", "tv", "anime"}
    ]
    download_rows = [
        row for row in details
        if row.get("kind") == "unmatched" and row.get("location") == "downloads"
    ]
    config = load_config(state.config_path)
    quarantined = quarantine_inventory(config)
    library_index = raw.get("library_index", []) if raw else []
    download_match_source = library_index or (raw.get("summary", []) if raw else [])
    return {
        "status": render_status(state),
        "latest": latest,
        "generated_at": raw.get("generated_at", "") if raw else "",
        "scan_breakdown": raw.get("scan_breakdown", []) if raw else [],
        "library_health": library_health_cards(raw.get("scan_breakdown", []) if raw else []),
        "download_candidates": download_cleanup_rows(download_rows, download_match_source, limit=5000),
        "download_summary": download_cleanup_summary(download_rows, download_match_source),
        "duplicate_candidates": dashboard_candidate_rows(duplicate_rows, limit=5000),
        "library_review": dashboard_candidate_rows(library_review_rows, limit=5000),
        "safe_candidates": dashboard_candidate_rows(library_review_rows, limit=5000),
        "quarantined": quarantined,
        "protections": {
            "qbittorrent_enabled": bool(config.get("qbittorrent", {}).get("enabled", False)),
            "jellyfin_enabled": bool(config.get("jellyfin", {}).get("enabled", False)),
            "radarr_enabled": bool(config.get("radarr", {}).get("enabled", False)),
            "sonarr_enabled": bool(config.get("sonarr", {}).get("enabled", False)),
        },
        "reports": report_names(raw, state),
    }


def raw_result_payload(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    summary = raw.get("summary", [])
    counts = raw.get("counts", {})
    safe_bytes = sum(int(row.get("reclaimable_bytes", 0) or 0) for row in summary)
    return {
        "stamp": raw.get("generated_at", ""),
        "files_scanned": int(counts.get("files_scanned", 0) or 0),
        "groups_count": int(counts.get("groups", 0) or 0),
        "unmatched_count": int(counts.get("unmatched", 0) or 0),
        "safe_count": sum(int(row.get("safe_cleanup_count", 0) or 0) for row in summary),
        "review_count": sum(int(row.get("review_count", 0) or 0) for row in summary),
        "reclaimable": human_size(safe_bytes),
    }


def dashboard_candidate_rows(rows: list[dict[str, Any]], limit: int = 250) -> list[dict[str, Any]]:
    candidates = []
    for row in rows[:limit]:
        candidates.append(
            {
                "path": row.get("path", ""),
                "title": row.get("title", "") or row.get("parsed_id", "") or row.get("path", ""),
                "size_human": row.get("size_human", ""),
                "size": row.get("size", 0),
                "keeper": row.get("keeper", ""),
                "keeper_size": row.get("keeper_size", ""),
                "keeper_size_human": row.get("keeper_size_human", ""),
                "kind": row.get("kind", ""),
                "location": row.get("location", ""),
                "match": "same title / same library item / larger duplicate"
                if row.get("recommendation") == "safe_cleanup_candidate"
                else row.get("parsed_id", "") or "not visible to Jellyfin/Sonarr/Radarr",
                "confidence": "High" if row.get("recommendation") == "safe_cleanup_candidate" else "Review",
                "reason": row.get("reason", ""),
            }
        )
    return candidates


def library_health_cards(scan_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = ["movies", "tv", "anime", "downloads"]
    by_location = {str(row.get("location", "")): row for row in scan_rows}
    cards = []
    for location in order:
        row = by_location.get(location, {})
        cards.append(
            {
                "location": location,
                "label": {"tv": "TV"}.get(location, location.title()),
                "file_count": int(row.get("file_count", 0) or 0),
                "total_size": row.get("total_size", "0 B"),
                "root": row.get("root", ""),
                "attention": location == "downloads" and int(row.get("file_count", 0) or 0) > 250,
            }
        )
    return cards


def download_cleanup_summary(download_rows: list[dict[str, Any]], summary_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = download_cleanup_rows(download_rows, summary_rows, limit=1000000)
    total = sum(int(row.get("size", 0) or 0) for row in rows)
    high = sum(1 for row in rows if row.get("confidence") == "High")
    likely_bytes = sum(int(row.get("size", 0) or 0) for row in rows if row.get("confidence") == "High")
    review = sum(1 for row in rows if row.get("confidence") != "High")
    old = sum(1 for row in rows if isinstance(row.get("age_days"), int) and row["age_days"] >= 14)
    return {
        "items": len(rows),
        "total_size": human_size(total),
        "high_confidence": high,
        "likely_reclaimable": human_size(likely_bytes),
        "review": review,
        "older_than_14_days": old,
    }


def download_cleanup_rows(
    download_rows: list[dict[str, Any]],
    library_rows: list[dict[str, Any]],
    limit: int = 300,
) -> list[dict[str, Any]]:
    library_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in library_rows:
        identity = str(row.get("identity") or row.get("title") or "")
        key = media_match_key(identity)
        if key:
            library_by_key[key].append(row)
    candidates = []
    for row in download_rows:
        parsed = str(row.get("parsed_id") or row.get("title") or row.get("path") or "")
        possible_type = str(row.get("possible_type") or "unknown")
        key = media_match_key(parsed)
        exact_matches = [
            match for match in library_by_key.get(key, [])
            if possible_type == "unknown" or not str(match.get("kind", "")) or str(match.get("kind", "")) == possible_type
        ]
        library_match = exact_matches[0] if len(exact_matches) == 1 else None
        confidence = "Review"
        bucket = "Unmatched download"
        if possible_type in {"episode", "movie"}:
            bucket = f"Likely {possible_type}"
        if library_match:
            confidence = "High"
            bucket = "Likely imported leftover"
        age = row.get("age_days", "")
        if confidence != "High" and isinstance(age, int) and age >= 14 and possible_type in {"episode", "movie"}:
            confidence = "Medium"
            bucket = f"Old {possible_type} download"
        candidates.append(
            {
                "path": row.get("path", ""),
                "title": parsed,
                "size": int(row.get("size", 0) or 0),
                "size_human": row.get("size_human", ""),
                "folder": row.get("folder", ""),
                "modified": row.get("modified", ""),
                "age_days": age,
                "possible_type": possible_type,
                "bucket": bucket,
                "match": bucket,
                "confidence": confidence,
                "keeper": library_match.get("path", "") if library_match else "",
                "keeper_size": int(library_match.get("size", 0) or 0) if library_match else 0,
                "keeper_size_human": library_match.get("size_human", "") if library_match else "",
                "reason": "Matching library copy found for the same parsed title and episode/year."
                if library_match
                else row.get("reason", ""),
            }
        )
    confidence_order = {"High": 0, "Medium": 1, "Review": 2}
    return sorted(
        candidates,
        key=lambda row: (
            confidence_order.get(str(row.get("confidence")), 9),
            -(row.get("age_days") if isinstance(row.get("age_days"), int) else -1),
            -int(row.get("size", 0) or 0),
        ),
    )[:limit]


def media_match_key(text: str) -> str:
    value = text.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def identity_key(text: str) -> str:
    value = text.lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\bs\d{1,2}e\d{1,3}\b", " ", value)
    value = re.sub(r"\b(19\d{2}|20\d{2})\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def report_names(raw: dict[str, Any] | None, state: DashboardState) -> dict[str, str]:
    latest = result_payload(state.last_result) if state.last_result else None
    if latest:
        return {key: latest[key] for key in ("html_report", "summary_csv", "details_csv", "raw_json")}
    files = sorted(state.output_dir.glob("media-cleanup-raw-*.json"), reverse=True)
    if not files:
        return {}
    stamp = files[0].stem.removeprefix("media-cleanup-raw-")
    return {
        "html_report": f"media-cleanup-report-{stamp}.html",
        "summary_csv": f"media-cleanup-summary-{stamp}.csv",
        "details_csv": f"media-cleanup-details-{stamp}.csv",
        "raw_json": files[0].name,
    }


def quarantine_root(config: dict[str, Any]) -> Path:
    root = str(config.get("media_roots", {}).get("erase_later", "/data/_erase_later")).strip()
    return Path(root)


def quarantine_manifest_path(config: dict[str, Any]) -> Path:
    return quarantine_root(config) / "mediacleanup-quarantine.json"


def read_quarantine_manifest(config: dict[str, Any]) -> list[dict[str, Any]]:
    path = quarantine_manifest_path(config)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def write_quarantine_manifest(config: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    root = quarantine_root(config)
    root.mkdir(parents=True, exist_ok=True)
    quarantine_readme(root)
    quarantine_manifest_path(config).write_text(json.dumps(rows, indent=2), encoding="utf-8")


def quarantine_readme(root: Path) -> None:
    readme = root / "README.txt"
    if readme.exists():
        return
    readme.write_text(
        "Media Cleanup quarantine folder\n\n"
        "Files here were moved by Media Cleanup after a user selected them in the dashboard.\n"
        "This is the middle safety stage between scan and permanent delete.\n"
        "Use the dashboard to restore files or permanently delete them after verification.\n",
        encoding="utf-8",
    )


def quarantine_inventory(config: dict[str, Any]) -> dict[str, Any]:
    rows = read_quarantine_manifest(config)
    active = [row for row in rows if row.get("status", "quarantined") == "quarantined"]
    total = sum(int(row.get("size", 0) or 0) for row in active)
    return {
        "empty": not active,
        "items": len(active),
        "recoverable_size": human_size(total),
        "rows": active,
    }


def latest_detail_map(state: DashboardState) -> dict[str, dict[str, Any]]:
    raw = latest_raw_payload(state) or {}
    return {str(row.get("path", "")): row for row in raw.get("details", []) if row.get("path")}


def quarantine_selected(state: DashboardState, paths: list[str], progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
    config = load_config(state.config_path)
    root = quarantine_root(config)
    allowed = latest_detail_map(state)
    manifest = read_quarantine_manifest(config)
    moved = []
    errors = []
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    total = len(paths)
    for index, path_text in enumerate(paths, 1):
        if progress:
            progress(index - 1, total, f"Checking {Path(path_text).name}")
        row = allowed.get(path_text)
        if not row:
            errors.append({"path": path_text, "error": "not found in latest audit details"})
            if progress:
                progress(index, total, f"Skipped {Path(path_text).name}")
            continue
        if str(row.get("protected_by_qbit", "")).lower() == "true":
            errors.append({"path": path_text, "error": "protected by qBittorrent"})
            if progress:
                progress(index, total, f"Skipped {Path(path_text).name}")
            continue
        source = Path(path_text)
        if not source.exists():
            errors.append({"path": path_text, "error": "file no longer exists"})
            if progress:
                progress(index, total, f"Missing {Path(path_text).name}")
            continue
        relative = str(source).replace("\\", "/").lstrip("/")
        dest = root / batch / relative
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            quarantine_readme(root)
            if progress:
                progress(index - 1, total, f"Moving {source.name}")
            shutil.move(str(source), str(dest))
        except OSError as exc:
            errors.append({"path": path_text, "error": str(exc)})
            if progress:
                progress(index, total, f"Failed {source.name}")
            continue
        record = {
            "id": f"{batch}:{len(manifest) + len(moved) + 1}",
            "original_path": str(source),
            "quarantine_path": str(dest),
            "size": int(row.get("size", 0) or 0),
            "size_human": row.get("size_human", ""),
            "title": row.get("title", ""),
            "reason": row.get("reason", ""),
            "moved_at": datetime.now().isoformat(timespec="seconds"),
            "status": "quarantined",
        }
        manifest.append(record)
        moved.append(record)
        if progress:
            progress(index, total, f"Moved {source.name}")
    if moved:
        write_quarantine_manifest(config, manifest)
    return {"moved": moved, "errors": errors, "quarantined": quarantine_inventory(config)}


def restore_quarantined(state: DashboardState, ids: list[str], progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
    config = load_config(state.config_path)
    manifest = read_quarantine_manifest(config)
    restored = []
    errors = []
    selected = set(ids)
    rows_to_process = [row for row in manifest if row.get("id") in selected and row.get("status", "quarantined") == "quarantined"]
    total = len(rows_to_process)
    for index, row in enumerate(rows_to_process, 1):
        source = Path(str(row.get("quarantine_path", "")))
        dest = Path(str(row.get("original_path", "")))
        if progress:
            progress(index - 1, total, f"Restoring {dest.name}")
        if not source.exists():
            errors.append({"id": row.get("id"), "error": "quarantined file is missing"})
            if progress:
                progress(index, total, f"Missing {dest.name}")
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
        except OSError as exc:
            errors.append({"id": row.get("id"), "error": str(exc)})
            if progress:
                progress(index, total, f"Failed {dest.name}")
            continue
        row["status"] = "restored"
        row["restored_at"] = datetime.now().isoformat(timespec="seconds")
        restored.append(row)
        if progress:
            progress(index, total, f"Restored {dest.name}")
    if restored:
        write_quarantine_manifest(config, manifest)
    return {"restored": restored, "errors": errors, "quarantined": quarantine_inventory(config)}


def delete_quarantined(state: DashboardState, ids: list[str], confirmation: str, progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
    if confirmation != "DELETE":
        return {"deleted": [], "errors": [{"error": "type DELETE to permanently delete selected files"}]}
    config = load_config(state.config_path)
    manifest = read_quarantine_manifest(config)
    deleted = []
    errors = []
    selected = set(ids)
    rows_to_process = [row for row in manifest if row.get("id") in selected and row.get("status", "quarantined") == "quarantined"]
    total = len(rows_to_process)
    for index, row in enumerate(rows_to_process, 1):
        path = Path(str(row.get("quarantine_path", "")))
        if progress:
            progress(index - 1, total, f"Deleting {path.name}")
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            errors.append({"id": row.get("id"), "error": str(exc)})
            if progress:
                progress(index, total, f"Failed {path.name}")
            continue
        row["status"] = "deleted"
        row["deleted_at"] = datetime.now().isoformat(timespec="seconds")
        deleted.append(row)
        if progress:
            progress(index, total, f"Deleted {path.name}")
    if deleted:
        write_quarantine_manifest(config, manifest)
    return {"deleted": deleted, "errors": errors, "quarantined": quarantine_inventory(config)}


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
            if parsed.path == "/action-status":
                self.send_json(action_status_payload(state))
                return
            if parsed.path == "/data":
                try:
                    self.send_json(dashboard_data(state))
                except Exception as exc:
                    self.send_json({"error": str(exc)}, status=500)
                return
            if parsed.path.startswith("/assets/"):
                self.send_asset_file(parsed.path.removeprefix("/assets/"))
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
            if parsed.path == "/quarantine":
                payload = self.read_json_body()
                self.send_json(start_dashboard_action(state, "quarantine", list(payload.get("paths", []))))
                return
            if parsed.path == "/restore":
                payload = self.read_json_body()
                self.send_json(start_dashboard_action(state, "restore", list(payload.get("ids", []))))
                return
            if parsed.path == "/delete":
                payload = self.read_json_body()
                self.send_json(start_dashboard_action(state, "delete", list(payload.get("ids", [])), str(payload.get("confirmation", ""))))
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

        def send_asset_file(self, name: str) -> None:
            safe_name = Path(urllib.parse.unquote(name)).name
            if safe_name not in {"dashboard.css", "dashboard.js"}:
                self.send_error(404)
                return
            path = Path(__file__).resolve().parent / safe_name
            if not path.exists() or not path.is_file():
                self.send_error(404)
                return
            content = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                return {}

    server = ThreadingHTTPServer((host, port), Handler)
    log(f"Media Cleanup dashboard listening on http://{host}:{port}")
    server.serve_forever()


def action_status_payload(state: DashboardState) -> dict[str, Any]:
    with state.action_lock:
        if not state.action_status:
            return {
                "id": state.action_id,
                "running": False,
                "kind": "",
                "current": 0,
                "total": 0,
                "percent": 0,
                "label": "",
                "result": None,
                "error": "",
            }
        return dict(state.action_status)


def set_action_status(
    state: DashboardState,
    *,
    running: bool,
    kind: str,
    current: int,
    total: int,
    label: str,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    percent = 100 if total <= 0 and not running else int((current / total) * 100) if total else 0
    percent = max(0, min(100, percent))
    with state.action_lock:
        state.action_status = {
            "id": state.action_id,
            "running": running,
            "kind": kind,
            "current": current,
            "total": total,
            "percent": percent,
            "label": label,
            "result": result,
            "error": error,
        }


def start_dashboard_action(
    state: DashboardState,
    kind: str,
    items: list[str],
    confirmation: str = "",
) -> dict[str, Any]:
    with state.action_lock:
        if state.action_status.get("running"):
            return {"started": False, "error": "another file action is already running", "action": dict(state.action_status)}
        state.action_id += 1
        action_id = state.action_id
        state.action_status = {
            "id": action_id,
            "running": True,
            "kind": kind,
            "current": 0,
            "total": len(items),
            "percent": 0,
            "label": "Starting",
            "result": None,
            "error": "",
        }

    def progress(current: int, total: int, label: str) -> None:
        set_action_status(state, running=True, kind=kind, current=current, total=total, label=label)

    def worker() -> None:
        try:
            if kind == "quarantine":
                result = quarantine_selected(state, items, progress)
            elif kind == "restore":
                result = restore_quarantined(state, items, progress)
            elif kind == "delete":
                result = delete_quarantined(state, items, confirmation, progress)
            else:
                result = {"errors": [{"error": f"unknown action {kind}"}]}
            set_action_status(state, running=False, kind=kind, current=len(items), total=len(items), label="Complete", result=result)
        except Exception as exc:
            set_action_status(state, running=False, kind=kind, current=0, total=len(items), label="Failed", result=None, error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, "action": action_status_payload(state)}


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


def render_dashboard_legacy(state: DashboardState) -> str:
    running = "true" if state.running else "false"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Media Cleanup</title>
  <style>
    :root {{
      --bg: #f4f6f8; --panel: #fff; --ink: #17202a; --muted: #667085;
      --line: #d9e0e8; --blue: #175cd3; --green: #067647; --amber: #a15c07;
      --red: #b42318; --soft: #eef4ff;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); -webkit-text-size-adjust: 100%; }}
    main {{ max-width: 1240px; margin: 0 auto; padding: 24px 24px 96px; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; position: sticky; top: 0; z-index: 10; background: rgba(244,246,248,.96); padding: 12px 0; backdrop-filter: blur(8px); }}
    h1 {{ margin: 0; font-size: 28px; }}
    h2 {{ margin: 0; font-size: 17px; }}
    .header-actions, .actions, .links {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    button, .link {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px 13px; min-height: 44px; background: #fff; color: var(--ink); font-weight: 800; cursor: pointer; text-decoration: none; touch-action: manipulation; }}
    .primary {{ background: var(--blue); color: #fff; border-color: var(--blue); }}
    .danger {{ background: var(--red); color: #fff; border-color: var(--red); }}
    button[disabled] {{ opacity: .55; cursor: wait; }}
    .sub, .meta {{ color: var(--muted); font-size: 13px; }}
    .layout {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; align-items: start; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }}
    .wide {{ grid-column: 1 / -1; }}
    .card-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 12px; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
    .stat {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fbfcfe; min-height: 76px; }}
    .stat.attention {{ border-color: #f79009; background: #fffbeb; }}
    .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; font-weight: 900; letter-spacing: .04em; }}
    .value {{ margin-top: 6px; font-size: 22px; font-weight: 900; overflow-wrap: anywhere; }}
    .green {{ color: var(--green); }} .amber {{ color: var(--amber); }} .red {{ color: var(--red); }}
    .list {{ display: grid; gap: 8px; max-height: 440px; overflow: auto; padding-right: 2px; -webkit-overflow-scrolling: touch; }}
    .item {{ display: grid; grid-template-columns: 28px 1fr; gap: 10px; border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fff; }}
    input[type="checkbox"] {{ width: 22px; height: 22px; margin-top: 2px; accent-color: var(--blue); }}
    .item-title {{ font-weight: 850; overflow-wrap: anywhere; }}
    .compare {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 8px; }}
    .compare-box {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fbfcfe; min-width: 0; }}
    .compare-box.quarantine {{ border-color: #fecdca; background: #fffbfa; }}
    .compare-box.keep {{ border-color: #abefc6; background: #f6fef9; }}
    .path {{ color: var(--muted); font-size: 12px; overflow-wrap: anywhere; margin-top: 4px; }}
    .pill {{ display: inline-flex; border-radius: 999px; background: var(--soft); color: var(--blue); font-size: 12px; font-weight: 850; padding: 3px 8px; margin-right: 6px; margin-top: 8px; }}
    .pill.high {{ background: #ecfdf3; color: var(--green); }}
    .pill.medium {{ background: #fffaeb; color: var(--amber); }}
    .notice {{ padding: 12px 14px; border-radius: 8px; background: #fff7ed; border: 1px solid #fed7aa; color: #7c2d12; }}
    .error {{ background: #fef3f2; border-color: #fecdca; color: var(--red); }}
    .mobile-tabs {{ display: none; }}
    .mobile-tabs button {{ flex: 1 1 auto; min-width: 88px; padding: 9px 10px; font-size: 12px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 999px; background: var(--green); display: inline-block; margin-right: 8px; }}
    .busy {{ background: var(--amber); animation: pulse 1s infinite; }}
    @keyframes pulse {{ 0%, 100% {{ opacity: .35 }} 50% {{ opacity: 1 }} }}
    @media (max-width: 900px) {{ main {{ padding: 12px 12px 108px; }} header {{ flex-direction: column; align-items: stretch; margin: 0 -12px 12px; padding: 12px; }} h1 {{ font-size: 24px; }} .layout, .stats, .compare {{ grid-template-columns: 1fr; }} .header-actions button {{ flex: 1; }} .card {{ padding: 13px; }} .card-head {{ align-items: stretch; flex-direction: column; }} .actions button {{ flex: 1 1 150px; }} .list {{ max-height: none; }} .path {{ font-size: 11px; }} .pill {{ font-size: 11px; max-width: 100%; }} .mobile-tabs {{ position: sticky; top: 92px; z-index: 9; display: flex; gap: 8px; overflow-x: auto; margin: 0 -12px 12px; padding: 8px 12px; background: rgba(244,246,248,.96); border-bottom: 1px solid var(--line); backdrop-filter: blur(8px); }} }}
    @media (max-width: 520px) {{ h1 {{ font-size: 22px; }} h2 {{ font-size: 16px; }} button, .link {{ width: 100%; }} .item {{ grid-template-columns: 34px 1fr; padding: 12px; }} input[type="checkbox"] {{ width: 26px; height: 26px; }} .compare-box {{ padding: 9px; }} .links a {{ flex: 1 1 100%; text-align: center; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Media Cleanup</h1>
        <div class="sub"><span id="dot" class="dot {'busy' if state.running else ''}"></span><span id="statusText">{'Running audit' if state.running else 'Ready'}</span> | <span id="timeText">{html.escape(render_time_text(state))}</span></div>
      </div>
      <div class="header-actions">
        <button id="run" class="primary" onclick="runAudit()">Run Audit</button>
        <button onclick="scrollToCard('quarantineCard')">Quarantined</button>
      </div>
    </header>
    <div id="message"></div>
    <nav class="mobile-tabs" aria-label="Cleanup sections">
      <button onclick="scrollToCard('downloadsCard')">Downloads</button>
      <button onclick="scrollToCard('duplicatesCard')">Duplicates</button>
      <button onclick="scrollToCard('safeCard')">Safe</button>
      <button onclick="scrollToCard('quarantineCard')">Quarantine</button>
    </nav>
    <section class="stats" id="libraryHealth"></section>
    <section class="layout" style="margin-top:14px;">
      <div class="card wide" id="downloadsCard">
        <div class="card-head">
          <div>
            <h2>Downloads Cleanup</h2>
            <div class="sub" id="downloadSummary">Waiting for audit.</div>
          </div>
          <span class="meta">largest cleanup target</span>
        </div>
        <div class="actions">
          <button onclick="reviewCard('downloads')">Review</button>
          <button class="primary" onclick="quarantineSelected('download')">Quarantine Selected</button>
        </div>
        <div class="list" id="downloads" style="margin-top:12px;"></div>
      </div>
      <div class="card" id="duplicatesCard">
        <div class="card-head"><h2>Scanned</h2><span class="meta" id="scanTotal"></span></div>
        <div id="scanned"></div>
      </div>
      <div class="card" id="quarantineCard">
        <div class="card-head"><h2>Quarantined</h2><span class="meta" id="quarantineSummary"></span></div>
        <div class="actions">
          <button onclick="restoreSelected()">Restore</button>
          <button class="danger" onclick="deleteSelected()">Delete Permanently</button>
        </div>
        <div class="list" id="quarantined" style="margin-top:12px;"></div>
      </div>
      <div class="card" id="safeCard">
        <div class="card-head"><h2>Duplicate Candidates</h2><span class="meta" id="duplicateSummary"></span></div>
        <div class="actions">
          <button onclick="reviewCard('duplicates')">Review</button>
          <button class="primary" onclick="quarantineSelected('duplicate')">Quarantine Selected</button>
        </div>
        <div class="list" id="duplicates" style="margin-top:12px;"></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Safe Candidates</h2><span class="meta" id="safeSummary"></span></div>
        <div class="actions">
          <button onclick="reviewCard('safe')">Review</button>
          <button class="primary" onclick="quarantineSelected('safe')">Quarantine Selected</button>
        </div>
        <div class="list" id="safe" style="margin-top:12px;"></div>
      </div>
      <div class="card wide">
        <div class="card-head"><h2>Read Me</h2><span class="meta">Scan -> Quarantine -> Permanent Delete</span></div>
        <div class="sub">No scan result is ever deleted directly. Selected items move to the quarantine folder first. Permanent delete is only available from Quarantined and requires typing DELETE.</div>
        <div class="links" id="links" style="margin-top:12px;"></div>
      </div>
    </section>
  </main>
  <script>
    let running = {running};
    let lastData = null;
    async function runAudit() {{
      const button = document.getElementById('run');
      button.disabled = true;
      document.getElementById('statusText').textContent = 'Starting audit';
      document.getElementById('dot').classList.add('busy');
      await fetch('/run', {{ method: 'POST' }});
      poll();
    }}
    async function poll() {{
      const res = await fetch('/data');
      const data = await res.json();
      render(data);
      if (data.status && data.status.running) setTimeout(poll, 1500);
    }}
    function render(data) {{
      lastData = data;
      const status = data.status || {{}};
      document.getElementById('run').disabled = !!status.running;
      document.getElementById('dot').classList.toggle('busy', !!status.running);
      document.getElementById('statusText').textContent = status.running ? 'Running audit' : 'Ready';
      document.getElementById('timeText').textContent = status.last_finished ? `Last finished ${{status.last_finished}}` : (status.last_started ? `Started ${{status.last_started}}` : 'No audit has run yet');
      document.getElementById('message').innerHTML = status.last_error ? `<div class="notice error">${{escapeHtml(status.last_error)}}</div>` : '';
      renderStats(data.latest);
      renderLibraryHealth(data.library_health || []);
      renderScanned(data.scan_breakdown || []);
      renderDownloads(data.download_candidates || [], data.download_summary || {{}});
      renderCandidates('duplicates', 'duplicateSummary', data.duplicate_candidates || [], 'duplicate');
      renderCandidates('safe', 'safeSummary', data.safe_candidates || [], 'safe');
      renderQuarantine(data.quarantined || {{ rows: [], empty: true, items: 0, recoverable_size: '0 B' }});
      renderLinks(data.reports || {{}});
    }}
    function renderStats(x) {{
      x = x || {{ files_scanned: 0, groups_count: 0, safe_count: 0, review_count: 0, reclaimable: '0 B' }};
      window.latestStats = x;
    }}
    function renderLibraryHealth(rows) {{
      const stats = window.latestStats || {{ safe_count: 0, review_count: 0, reclaimable: '0 B' }};
      const health = rows.length ? rows.map(row => `
        <div class="stat ${{row.attention ? 'attention' : ''}}">
          <div class="label">${{escapeHtml(row.label)}}</div>
          <div class="value">${{row.file_count}}</div>
          <div class="sub">${{escapeHtml(row.total_size || '0 B')}}</div>
        </div>`).join('') : '';
      document.getElementById('libraryHealth').innerHTML = health + `
        <div class="stat"><div class="label">Candidates</div><div class="value green">${{stats.safe_count || 0}}</div><div class="sub">${{escapeHtml(stats.reclaimable || '0 B')}}</div></div>`;
    }}
    function renderScanned(rows) {{
      const total = rows.reduce((sum, row) => sum + Number(row.file_count || 0), 0);
      document.getElementById('scanTotal').textContent = `${{total}} files`;
      document.getElementById('scanned').innerHTML = rows.length ? rows.map(row => `
        <div class="item" style="grid-template-columns:1fr;">
          <div><div class="item-title">${{titleCase(row.location)}}: ${{row.file_count}} files</div>
          <div class="path">${{escapeHtml(row.root || '')}} | ${{escapeHtml(row.total_size || '0 B')}}</div></div>
        </div>`).join('') : `<div class="notice">No audit has run yet.</div>`;
    }}
    function renderCandidates(target, summaryTarget, rows, prefix) {{
      document.getElementById(summaryTarget).textContent = `${{rows.length}} items`;
      document.getElementById(target).innerHTML = rows.length ? rows.map((row, index) => `
        <label class="item">
          <input type="checkbox" data-kind="${{prefix}}" value="${{escapeAttr(row.path)}}">
          <div>
            <div class="item-title">${{escapeHtml(row.title || row.path)}}</div>
            ${{row.keeper ? compareHtml(row) : `<div class="path">${{escapeHtml(row.path)}}</div>`}}
            <span class="pill">Size: ${{escapeHtml(row.size_human || 'unknown')}}</span>
            <span class="pill">Match: ${{escapeHtml(row.match || 'review')}}</span>
            <span class="pill">Confidence: ${{escapeHtml(row.confidence || 'Review')}}</span>
          </div>
        </label>`).join('') : `<div class="notice">No rows.</div>`;
    }}
    function compareHtml(row) {{
      return `<div class="compare">
        <div class="compare-box quarantine">
          <div class="label">Quarantine This</div>
          <div class="item-title">${{escapeHtml(row.size_human || 'unknown')}}</div>
          <div class="path">${{escapeHtml(row.path || '')}}</div>
        </div>
        <div class="compare-box keep">
          <div class="label">Keep This</div>
          <div class="item-title">${{escapeHtml(row.keeper_size_human || 'unknown')}}</div>
          <div class="path">${{escapeHtml(row.keeper || '')}}</div>
        </div>
      </div>`;
    }}
    function renderDownloads(rows, summary) {{
      document.getElementById('downloadSummary').textContent =
        `${{summary.items || 0}} items | ${{summary.total_size || '0 B'}} | high confidence: ${{summary.high_confidence || 0}} | older than 14 days: ${{summary.older_than_14_days || 0}}`;
      document.getElementById('downloads').innerHTML = rows.length ? rows.map(row => `
        <label class="item">
          <input type="checkbox" data-kind="download" value="${{escapeAttr(row.path)}}">
          <div>
            <div class="item-title">${{escapeHtml(row.title || row.path)}}</div>
            <div class="path">${{escapeHtml(row.folder || row.path)}}</div>
            <span class="pill ${{String(row.confidence).toLowerCase()}}">Confidence: ${{escapeHtml(row.confidence || 'Review')}}</span>
            <span class="pill">Bucket: ${{escapeHtml(row.bucket || 'download')}}</span>
            <span class="pill">Size: ${{escapeHtml(row.size_human || 'unknown')}}</span>
            <span class="pill">Age: ${{row.age_days === '' ? 'unknown' : `${{row.age_days}} days`}}</span>
          </div>
        </label>`).join('') : `<div class="notice">No download cleanup rows yet. Run Audit to refresh.</div>`;
    }}
    function renderQuarantine(q) {{
      document.getElementById('quarantineSummary').textContent = `Empty? ${{q.empty ? 'Yes' : 'No'}} | Items: ${{q.items || 0}} | Recoverable size: ${{q.recoverable_size || '0 B'}}`;
      const rows = q.rows || [];
      document.getElementById('quarantined').innerHTML = rows.length ? rows.map(row => `
        <label class="item">
          <input type="checkbox" data-kind="quarantine" value="${{escapeAttr(row.id)}}">
          <div>
            <div class="item-title">${{escapeHtml(row.title || row.original_path)}}</div>
            <div class="path">From: ${{escapeHtml(row.original_path || '')}}</div>
            <div class="path">Now: ${{escapeHtml(row.quarantine_path || '')}}</div>
            <span class="pill">${{escapeHtml(row.size_human || '')}}</span>
            <span class="pill">Moved: ${{escapeHtml(row.moved_at || '')}}</span>
          </div>
        </label>`).join('') : `<div class="notice">Quarantine is empty.</div>`;
    }}
    function renderLinks(reports) {{
      if (!reports.raw_json) {{
        document.getElementById('links').innerHTML = `<span class="sub">Reports will appear after the first audit.</span>`;
        return;
      }}
      document.getElementById('links').innerHTML = `
        <a class="link" href="/reports/${{encodeURIComponent(reports.html_report)}}" target="_blank">Open HTML</a>
        <a class="link" href="/reports/${{encodeURIComponent(reports.summary_csv)}}">Summary CSV</a>
        <a class="link" href="/reports/${{encodeURIComponent(reports.details_csv)}}">Details CSV</a>
        <a class="link" href="/reports/${{encodeURIComponent(reports.raw_json)}}">Raw JSON</a>`;
    }}
    async function quarantineSelected(kind) {{
      const paths = selected(kind);
      if (!paths.length) return showMessage('Select at least one item first.', false);
      await postAction('/quarantine', {{ paths }});
    }}
    async function restoreSelected() {{
      const ids = selected('quarantine');
      if (!ids.length) return showMessage('Select at least one quarantined item first.', false);
      await postAction('/restore', {{ ids }});
    }}
    async function deleteSelected() {{
      const ids = selected('quarantine');
      if (!ids.length) return showMessage('Select at least one quarantined item first.', false);
      const confirmation = prompt(`Type DELETE to permanently remove ${{ids.length}} files.`);
      if (confirmation !== 'DELETE') return showMessage('Permanent delete cancelled.', false);
      await postAction('/delete', {{ ids, confirmation }});
    }}
    async function postAction(url, payload) {{
      const res = await fetch(url, {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }});
      const data = await res.json();
      if (data.errors && data.errors.length) showMessage(data.errors.map(e => e.error || JSON.stringify(e)).join(' | '), true);
      else showMessage('Done.', false);
      await poll();
    }}
    function selected(kind) {{
      return Array.from(document.querySelectorAll(`input[data-kind="${{kind}}"]:checked`)).map(input => input.value);
    }}
    function reviewCard(id) {{ document.getElementById(id).scrollIntoView({{ behavior: 'smooth', block: 'start' }}); }}
    function scrollToCard(id) {{ document.getElementById(id).scrollIntoView({{ behavior: 'smooth', block: 'start' }}); }}
    function showMessage(text, error) {{
      document.getElementById('message').innerHTML = `<div class="notice ${{error ? 'error' : ''}}">${{escapeHtml(text)}}</div>`;
    }}
    function escapeHtml(s) {{
      return String(s || '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    function escapeAttr(s) {{ return escapeHtml(s).replace(/`/g, '&#96;'); }}
    function titleCase(s) {{ return String(s || '').replace(/_/g, ' ').replace(/\\b\\w/g, c => c.toUpperCase()); }}
    poll();
    if (running) setTimeout(poll, 1500);
  </script>
</body>
</html>"""


def render_dashboard(state: DashboardState) -> str:
    template_path = Path(__file__).resolve().parent / "dashboard.html"
    if not template_path.exists():
        return render_dashboard_legacy(state)
    page = template_path.read_text(encoding="utf-8")
    return (
        page.replace("__RUNNING__", "true" if state.running else "false")
        .replace("__BUSY_CLASS__", "busy" if state.running else "")
        .replace("__STATUS_TEXT__", "Running audit" if state.running else "Ready")
        .replace("__TIME_TEXT__", html.escape(render_time_text(state)))
    )


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

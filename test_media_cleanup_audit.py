import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from media_cleanup_audit import (
    AuditResult,
    DashboardState,
    MediaGroup,
    VideoFile,
    action_status_payload,
    classify_groups,
    canonicalize_path,
    classify_unmatched,
    dashboard_candidate_rows,
    dashboard_path_is_live,
    delete_quarantined,
    download_cleanup_rows,
    gather_episode_candidates,
    library_health_cards,
    parse_media_identity,
    render_dashboard,
    render_status,
    resolve_media_file_path,
    scan_breakdown_rows,
    scan_error_rows,
    unmatched_breakdown_rows,
    validate_config,
    fetch_jellyfin_user_id,
    fetch_sonarr,
    issue_dashboard_session,
    library_index_rows,
    move_file,
    quarantine_destination,
    quarantine_storage_status,
    scan_video_files,
    storage_volume_rows,
    start_dashboard_action,
    run_audit,
    valid_dashboard_session,
)


def vf(path, size, inode, qbit=False, jellyfin=False):
    return VideoFile(
        path=path,
        norm_path=path.lower(),
        size=size,
        device=1,
        inode=inode,
        nlink=1,
        source_root="/data",
        protected_by_qbit=qbit,
        jellyfin_visible=jellyfin,
    )


def write_action_config(root: Path) -> Path:
    config = root / "config.yml"
    qroot = (root / "quarantine").as_posix()
    config.write_text(
        "\n".join(
            [
                "media_roots:",
                f"  erase_later: {qroot}",
                "scan:",
                "  roots:",
                f"    - {qroot}",
                "jellyfin:",
                "  enabled: false",
                "radarr:",
                "  enabled: false",
                "sonarr:",
                "  enabled: false",
                "qbittorrent:",
                "  enabled: false",
            ]
        ),
        encoding="utf-8",
    )
    return config


def write_quarantined_file(root: Path) -> tuple[Path, dict]:
    qroot = root / "quarantine"
    qfile = qroot / "batch" / "movie.mkv"
    qfile.parent.mkdir(parents=True)
    qfile.write_bytes(b"media")
    row = {
        "id": "batch:1",
        "original_path": (root / "movies" / "movie.mkv").as_posix(),
        "quarantine_path": qfile.as_posix(),
        "size": 5,
        "size_human": "5 B",
        "title": "movie",
        "status": "quarantined",
    }
    (qroot / "mediacleanup-quarantine.json").write_text(json.dumps([row]), encoding="utf-8")
    return qfile, row


class MediaCleanupAuditTests(unittest.TestCase):
    def test_resolves_relative_radarr_sonarr_paths(self):
        self.assertEqual(
            resolve_media_file_path("/data/media/movies/Arrival (2016)", {"relativePath": "Arrival.2016.mkv"}),
            "/data/media/movies/Arrival (2016)/Arrival.2016.mkv",
        )

    def test_episode_matching_does_not_collect_whole_season(self):
        files = [
            vf("/data/media/tv/Show/Season 01/Show.S01E01.720p.mkv", 1, 1),
            vf("/data/media/tv/Show/Season 01/Show.S01E02.720p.mkv", 1, 2),
        ]
        matches = gather_episode_candidates(
            files,
            "/data/media/tv/Show/Season 01/Show.S01E01.720p.mkv",
            "/data/media/tv/Show",
            [{"seasonNumber": 1, "episodeNumber": 1}],
        )
        self.assertEqual([m.path for m in matches], ["/data/media/tv/Show/Season 01/Show.S01E01.720p.mkv"])

    def test_safe_cleanup_requires_unprotected_larger_distinct_file(self):
        config = {"media_roots": {"movies": "/data/media/movies", "tv": "/data/media/tv"}}
        keeper = vf("/data/media/movies/Arrival (2016)/Arrival.720p.mkv", 100, 1, jellyfin=True)
        larger = vf("/data/media/movies/Arrival (2016)/Arrival.1080p.mkv", 200, 2)
        group = MediaGroup("radarr:1", "movie", "Arrival", "/data/media/movies/Arrival (2016)", "1", [keeper, larger])
        summary, details = classify_groups(config, [group])
        self.assertEqual(summary[0]["safe_cleanup_count"], 1)
        self.assertEqual(details[0]["recommendation"], "safe_cleanup_candidate")

    def test_qbit_protected_duplicate_goes_to_review(self):
        config = {"media_roots": {"movies": "/data/media/movies", "tv": "/data/media/tv"}}
        keeper = vf("/data/media/movies/Arrival (2016)/Arrival.720p.mkv", 100, 1, jellyfin=True)
        larger = vf("/data/media/movies/Arrival (2016)/Arrival.1080p.mkv", 200, 2, qbit=True)
        group = MediaGroup("radarr:1", "movie", "Arrival", "/data/media/movies/Arrival (2016)", "1", [keeper, larger])
        summary, details = classify_groups(config, [group])
        self.assertEqual(summary[0]["safe_cleanup_count"], 0)
        self.assertEqual(details[0]["recommendation"], "review")

    def test_config_validation_reports_missing_enabled_api_key(self):
        with self.assertRaisesRegex(ValueError, "jellyfin.api_key"):
            validate_config(
                {
                    "scan": {"roots": ["/data"]},
                    "jellyfin": {"enabled": True, "url": "http://jellyfin:8096"},
                    "radarr": {"enabled": False},
                    "sonarr": {"enabled": False},
                    "qbittorrent": {"enabled": False},
                }
            )

    def test_dashboard_renders_run_button_and_latest_report_area(self):
        state = DashboardState(config_path="/app/config.yml", output_dir=Path("/reports"))
        body = render_dashboard(state)
        self.assertIn("Run audit", body)
        self.assertIn("Duplicates", body)
        self.assertIn("Quarantine", body)
        self.assertIn("/assets/dashboard.css", body)
        self.assertIn("/assets/dashboard.js", body)
        self.assertIn("Library review", body)
        self.assertIn("downloadBrief", body)
        self.assertIn("Select matches", body)
        self.assertIn("actionProgress", body)
        self.assertIn("auth/logout", body)

    def test_dashboard_password_session_requires_the_current_password(self):
        state = DashboardState(config_path="/app/config.yml", output_dir=Path("/reports"), dashboard_password="long-password")
        token = issue_dashboard_session(state)
        self.assertTrue(valid_dashboard_session(state, f"mediacleanup_session={token}"))
        self.assertFalse(valid_dashboard_session(state, "mediacleanup_session=not-a-session"))
        state.dashboard_password = "changed-password"
        self.assertFalse(valid_dashboard_session(state, f"mediacleanup_session={token}"))

    def test_dashboard_status_includes_latest_report_names(self):
        result = AuditResult(
            stamp="20260708-120000",
            output_dir=Path("/reports"),
            summary_rows=[{"safe_cleanup_count": 2, "review_count": 1, "reclaimable_bytes": 1000}],
            detail_rows=[],
            diagnostic_rows=[],
            files_scanned=10,
            groups_count=3,
            unmatched_count=4,
            summary_csv=Path("/reports/summary.csv"),
            details_csv=Path("/reports/details.csv"),
            html_report=Path("/reports/report.html"),
            raw_json=Path("/reports/raw.json"),
        )
        state = DashboardState(config_path="/app/config.yml", output_dir=Path("/reports"), last_result=result)
        status = render_status(state)
        self.assertEqual(status["latest"]["safe_count"], 2)
        self.assertEqual(status["latest"]["html_report"], "report.html")

    def test_jellyfin_user_picker_skips_disabled_users(self):
        import media_cleanup_audit

        original = media_cleanup_audit.api_get
        try:
            media_cleanup_audit.api_get = lambda *args, **kwargs: [
                {"Id": "disabled", "Policy": {"IsDisabled": True}},
                {"Id": "enabled", "Policy": {"IsDisabled": False}},
            ]
            self.assertEqual(fetch_jellyfin_user_id("http://jellyfin:8096", {}), "enabled")
        finally:
            media_cleanup_audit.api_get = original

    def test_sonarr_episode_files_are_fetched_by_series(self):
        import media_cleanup_audit

        calls = []
        original = media_cleanup_audit.api_get

        def fake_api_get(url, headers=None, params=None, label="API request"):
            calls.append((url, params or {}))
            if url.endswith("/api/v3/series"):
                return [{"id": 10, "title": "Show", "path": "/data/media/tv/Show"}]
            if url.endswith("/api/v3/episode"):
                return [{"id": 20, "seriesId": 10, "episodeFileId": 30, "seasonNumber": 1, "episodeNumber": 1}]
            if url.endswith("/api/v3/episodefile"):
                return [{"id": 30, "relativePath": "Season 01/Show.S01E01.mkv"}]
            return []

        try:
            media_cleanup_audit.api_get = fake_api_get
            data = fetch_sonarr({"sonarr": {"enabled": True, "url": "http://sonarr:8989", "api_key": "key"}})
            self.assertIn(30, data["episode_files"])
            self.assertIn(("http://sonarr:8989/api/v3/episodefile", {"seriesId": 10}), calls)
            self.assertFalse(any("episodeFileIds" in params for _, params in calls))
        finally:
            media_cleanup_audit.api_get = original

    def test_path_mappings_canonicalize_app_paths(self):
        config = {
            "path_mappings": [
                {"from": "/movies", "to": "/data/movies"},
                {"from": "/tvshows", "to": "/data/tvshows"},
            ]
        }
        self.assertEqual(
            canonicalize_path(config, "/movies/Arrival (2016)/Arrival.mkv"),
            "/data/movies/arrival (2016)/arrival.mkv",
        )
        self.assertEqual(
            canonicalize_path(config, "/tvshows/Show/Season 01/Show.S01E01.mkv"),
            "/data/tvshows/show/season 01/show.s01e01.mkv",
        )

    def test_scan_errors_become_review_rows(self):
        rows = scan_error_rows([{"path": "/data/downloads/locked.mkv", "reason": "permission denied"}])
        self.assertEqual(rows[0]["kind"], "inaccessible")
        self.assertEqual(rows[0]["recommendation"], "review")
        self.assertIn("permission denied", rows[0]["reason"])

    def test_unmatched_library_file_is_zombie_candidate(self):
        config = {"media_roots": {"movies": "/data/movies", "tv": "/data/tvshows", "downloads": "/data/downloads"}}
        item = vf("/data/movies/Arrival (2016)/Arrival.2016.1080p.mkv", 100, 1)
        classification = classify_unmatched(config, item)
        self.assertEqual(classification["location"], "movies")
        self.assertEqual(classification["possible_type"], "movie")
        self.assertIn("orphan/zombie", classification["reason"])

    def test_parse_media_identity_for_episode_and_movie(self):
        self.assertEqual(
            parse_media_identity("/data/downloads/Show.Name.S02E03.720p.mkv")["parsed_id"],
            "Show Name S02E03",
        )
        self.assertEqual(
            parse_media_identity("/data/downloads/Arrival.2016.1080p.mkv")["parsed_id"],
            "Arrival (2016)",
        )

    def test_unmatched_breakdown_totals_by_location_and_type(self):
        rows = [
            {"location": "downloads", "possible_type": "episode", "size": 100},
            {"location": "downloads", "possible_type": "episode", "size": 300},
        ]
        breakdown = unmatched_breakdown_rows(rows)
        self.assertEqual(breakdown[0]["file_count"], 2)
        self.assertEqual(breakdown[0]["total_bytes"], 400)

    def test_scan_breakdown_counts_configured_roots(self):
        config = {
            "media_roots": {"movies": "/data/movies", "tv": "/data/tvshows", "downloads": "/data/downloads"},
            "scan": {"roots": ["/data/movies", "/data/tvshows", "/data/anime", "/data/downloads"]},
        }
        rows = scan_breakdown_rows(
            config,
            [
                vf("/data/movies/Arrival/Arrival.mkv", 100, 1),
                vf("/data/tvshows/Show/Show.S01E01.mkv", 200, 2),
                vf("/data/anime/Series/E01.mkv", 300, 3),
            ],
        )
        by_location = {row["location"]: row for row in rows}
        self.assertEqual(by_location["movies"]["file_count"], 1)
        self.assertEqual(by_location["tv"]["file_count"], 1)
        self.assertEqual(by_location["anime"]["file_count"], 1)

    def test_permanent_delete_requires_confirmation(self):
        state = DashboardState(config_path="/app/config.yml", output_dir=Path("/reports"))
        result = delete_quarantined(state, ["anything"], "delete")
        self.assertEqual(result["deleted"], [])
        self.assertIn("DELETE", result["errors"][0]["error"])

    def test_delete_quarantined_removes_file_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_action_config(root)
            qfile, row = write_quarantined_file(root)
            state = DashboardState(config_path=str(config), output_dir=root / "reports")
            calls = []
            result = delete_quarantined(state, [row["id"]], "DELETE", lambda current, total, label: calls.append((current, total, label)))
            self.assertFalse(qfile.exists())
            self.assertEqual(result["deleted"][0]["id"], row["id"])
            self.assertEqual(calls[-1][0], 1)
            self.assertEqual(calls[-1][1], 1)

    def test_dashboard_action_status_tracks_delete_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_action_config(root)
            qfile, row = write_quarantined_file(root)
            state = DashboardState(config_path=str(config), output_dir=root / "reports")
            started = start_dashboard_action(state, "delete", [row["id"]], "DELETE")
            self.assertTrue(started["started"])
            status = action_status_payload(state)
            for _ in range(100):
                status = action_status_payload(state)
                if not status["running"]:
                    break
                time.sleep(0.01)
            self.assertFalse(status["running"])
            self.assertEqual(status["percent"], 100)
            self.assertFalse(qfile.exists())
            self.assertEqual(status["result"]["deleted"][0]["id"], row["id"])

    def test_fast_local_quarantine_stays_beside_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            source = downloads / "show" / "episode.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"media")
            config = {
                "media_roots": {"downloads": downloads.as_posix(), "erase_later": (root / "central").as_posix()},
                "quarantine": {"local_fast_path": True},
            }
            destination_root, source_root = quarantine_destination(config, source)
            self.assertEqual(destination_root, downloads / ".mediacleanup-quarantine")
            self.assertEqual(source_root, downloads)

            destination = destination_root / "batch" / source.relative_to(source_root)
            destination.parent.mkdir(parents=True)
            self.assertEqual(move_file(source, destination), "instant")
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"media")

    def test_quarantine_defaults_to_the_source_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            source = downloads / "movie.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"media")
            config = {"media_roots": {"downloads": downloads.as_posix(), "erase_later": (root / "local-server").as_posix()}}
            destination_root, source_root = quarantine_destination(config, source)
            self.assertEqual(destination_root, downloads / ".mediacleanup-quarantine")
            self.assertEqual(source_root, downloads)

    def test_storage_safety_reports_ready_for_writable_control_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "control"
            root.mkdir()
            status = quarantine_storage_status({"media_roots": {"erase_later": root.as_posix()}})
            self.assertTrue(status["ready"])
            self.assertTrue(status["fast_local"])

    def test_storage_safety_warns_when_fast_quarantine_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "control"
            root.mkdir()
            status = quarantine_storage_status(
                {"media_roots": {"erase_later": root.as_posix()}, "quarantine": {"local_fast_path": False}}
            )
            self.assertFalse(status["ready"])
            self.assertIn("off", status["message"])

    def test_storage_volume_rows_groups_roots_on_the_same_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movies = root / "movies"
            downloads = root / "downloads"
            movies.mkdir()
            downloads.mkdir()
            rows = storage_volume_rows({"media_roots": {"movies": movies.as_posix(), "downloads": downloads.as_posix()}})
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["label"], "Movies / Downloads")
            self.assertGreater(rows[0]["total"], 0)

    def test_dashboard_hides_stale_and_quarantined_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            live = downloads / "live.mkv"
            held = downloads / ".mediacleanup-quarantine" / "batch" / "held.mkv"
            live.parent.mkdir(parents=True)
            held.parent.mkdir(parents=True)
            live.write_bytes(b"live")
            held.write_bytes(b"held")
            config = {"quarantine": {"local_fast_path": True}}
            self.assertTrue(dashboard_path_is_live(config, {"path": live.as_posix()}))
            self.assertFalse(dashboard_path_is_live(config, {"path": held.as_posix()}))
            self.assertFalse(dashboard_path_is_live(config, {"path": (downloads / "missing.mkv").as_posix()}))

    def test_qbittorrent_failure_does_not_abort_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.yml"
            config.write_text(
                "\n".join(
                    [
                        "media_roots:",
                        f"  downloads: {root.as_posix()}",
                        "scan:",
                        "  roots:",
                        f"    - {root.as_posix()}",
                        "jellyfin:",
                        "  enabled: false",
                        "radarr:",
                        "  enabled: false",
                        "sonarr:",
                        "  enabled: false",
                        "qbittorrent:",
                        "  enabled: true",
                        "  url: http://qbittorrent:8080",
                        "  username: user",
                        "  password: pass",
                    ]
                ),
                encoding="utf-8",
            )
            with patch("media_cleanup_audit.fetch_qbit_paths", side_effect=RuntimeError("login failed")):
                result = run_audit(str(config), root / "reports")
            raw = json.loads(result.raw_json.read_text(encoding="utf-8"))
            self.assertFalse(raw["qbittorrent_status"]["available"])
            self.assertIn("login failed", raw["qbittorrent_status"]["error"])

    def test_fast_local_quarantine_is_not_scanned_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            (downloads / "normal.mkv").parent.mkdir(parents=True)
            (downloads / "normal.mkv").write_bytes(b"normal")
            held = downloads / ".mediacleanup-quarantine" / "batch" / "held.mkv"
            held.parent.mkdir(parents=True)
            held.write_bytes(b"held")
            config = {"media_roots": {"downloads": downloads.as_posix()}, "scan": {"roots": [downloads.as_posix()]}}
            scanned = scan_video_files(config)
            self.assertEqual([Path(item.path).name for item in scanned.files], ["normal.mkv"])

    def test_download_cleanup_marks_imported_leftovers_high_confidence(self):
        rows = download_cleanup_rows(
            [
                {
                    "path": "/data/downloads/Show.Name.S02E03.mkv",
                    "parsed_id": "Show Name S02E03",
                    "possible_type": "episode",
                    "size": 100,
                    "age_days": 20,
                }
            ],
            [{"title": "Show Name S02E03"}],
        )
        self.assertEqual(rows[0]["confidence"], "High")
        self.assertEqual(rows[0]["bucket"], "Likely imported leftover")

    def test_download_cleanup_does_not_cross_match_episode_numbers(self):
        rows = download_cleanup_rows(
            [
                {
                    "path": "/data/downloads/Show.Name.S02E03.mkv",
                    "parsed_id": "Show Name S02E03",
                    "possible_type": "episode",
                    "size": 100,
                }
            ],
            [
                {
                    "kind": "episode",
                    "identity": "Show Name S02E04",
                    "path": "/data/tvshows/Show/Season 02/Show.Name.S02E04.mkv",
                    "size": 80,
                    "size_human": "80 B",
                }
            ],
        )
        self.assertEqual(rows[0]["confidence"], "Review")
        self.assertEqual(rows[0]["keeper"], "")

    def test_download_cleanup_exact_library_match_includes_keeper(self):
        rows = download_cleanup_rows(
            [
                {
                    "path": "/data/downloads/Show.Name.S02E03.1080p.mkv",
                    "parsed_id": "Show Name S02E03",
                    "possible_type": "episode",
                    "size": 200,
                }
            ],
            [
                {
                    "kind": "episode",
                    "identity": "Show Name S02E03",
                    "path": "/data/tvshows/Show/Season 02/Show.Name.S02E03.720p.mkv",
                    "size": 100,
                    "size_human": "100 B",
                }
            ],
        )
        self.assertEqual(rows[0]["confidence"], "High")
        self.assertEqual(rows[0]["keeper"], "/data/tvshows/Show/Season 02/Show.Name.S02E03.720p.mkv")
        self.assertEqual(rows[0]["keeper_size_human"], "100 B")

    def test_library_health_flags_large_downloads(self):
        cards = library_health_cards([{"location": "downloads", "file_count": 3314, "total_size": "7.1 TB"}])
        downloads = [card for card in cards if card["location"] == "downloads"][0]
        self.assertTrue(downloads["attention"])

    def test_dashboard_duplicate_candidates_include_keeper_compare_data(self):
        rows = dashboard_candidate_rows(
            [
                {
                    "path": "/data/downloads/Arrival.1080p.mkv",
                    "title": "Arrival",
                    "size_human": "4.2 GB",
                    "keeper": "/data/movies/Arrival/Arrival.720p.mkv",
                    "keeper_size_human": "1.4 GB",
                    "recommendation": "safe_cleanup_candidate",
                    "kind": "movie",
                }
            ]
        )
        self.assertEqual(rows[0]["keeper"], "/data/movies/Arrival/Arrival.720p.mkv")
        self.assertEqual(rows[0]["keeper_size_human"], "1.4 GB")

    def test_library_index_uses_smallest_expected_library_file(self):
        group = MediaGroup(
            "sonarr:1",
            "episode",
            "Show Name S02E03",
            "/data/tvshows/Show",
            "1",
            [
                vf("/data/tvshows/Show/Season 02/Show.Name.S02E03.1080p.mkv", 200, 1),
                vf("/data/tvshows/Show/Season 02/Show.Name.S02E03.720p.mkv", 100, 2, jellyfin=True),
                vf("/data/downloads/Show.Name.S02E03.1080p.mkv", 200, 3),
            ],
        )
        rows = library_index_rows([group])
        self.assertEqual(rows[0]["path"], "/data/tvshows/Show/Season 02/Show.Name.S02E03.720p.mkv")
        self.assertEqual(rows[0]["identity"], "Show Name S02E03")


if __name__ == "__main__":
    unittest.main()

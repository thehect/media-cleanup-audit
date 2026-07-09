import unittest
from pathlib import Path

from media_cleanup_audit import (
    AuditResult,
    DashboardState,
    MediaGroup,
    VideoFile,
    classify_groups,
    gather_episode_candidates,
    render_dashboard,
    render_status,
    resolve_media_file_path,
    validate_config,
    fetch_jellyfin_user_id,
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
        self.assertIn("Run Audit", body)
        self.assertIn("Latest Report", body)

    def test_dashboard_status_includes_latest_report_names(self):
        result = AuditResult(
            stamp="20260708-120000",
            output_dir=Path("/reports"),
            summary_rows=[{"safe_cleanup_count": 2, "review_count": 1, "reclaimable_bytes": 1000}],
            detail_rows=[],
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


if __name__ == "__main__":
    unittest.main()

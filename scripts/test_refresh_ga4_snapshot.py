#!/usr/bin/env python3
"""Tests for the GA4 refresh script that do not call Google or need a key."""

import datetime as dt
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = Path(__file__).resolve().parent / "refresh_ga4_snapshot.py"


def load_module():
    spec = importlib.util.spec_from_file_location("refresh_ga4_snapshot", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


refresh = load_module()


def row(dimensions, metrics):
    return {
        "dimension_values": [{"value": value} for value in dimensions],
        "metric_values": [{"value": str(value)} for value in metrics],
    }


class RefreshTests(unittest.TestCase):
    def test_official_mcp_name(self):
        self.assertEqual(refresh.OFFICIAL_MCP_SERVER, "analytics-mcp")
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("analytics-mcp", source)
        self.assertNotIn("private_key", source.split("parse_service_account", 1)[0])

    def test_credentials_come_only_from_env_json(self):
        with self.assertRaises(refresh.RefreshError) as empty:
            refresh.parse_service_account("")
        self.assertIn("GA4_SERVICE_ACCOUNT_JSON", str(empty.exception))
        with self.assertRaises(refresh.RefreshError) as path:
            refresh.parse_service_account("/tmp/secret.json")
        self.assertNotIn("/tmp/secret.json", str(path.exception))
        with self.assertRaises(refresh.RefreshError):
            refresh.parse_service_account("{not json")
        info = refresh.parse_service_account(
            json.dumps(
                {
                    "type": "service_account",
                    "client_email": "reader@example.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n",
                    "project_id": "example-project",
                }
            )
        )
        self.assertEqual(info["client_email"], "reader@example.iam.gserviceaccount.com")

    def test_credential_file_is_temporary_and_removed(self):
        info = {
            "type": "service_account",
            "client_email": "reader@example.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nstored-only-in-temp\n",
            "project_id": "example-project",
        }
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        with refresh.ServiceAccountCredentials(info) as creds:
            path = creds.path
            self.assertTrue(path.is_file())
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(os.environ["GOOGLE_APPLICATION_CREDENTIALS"], str(path))
            self.assertEqual(os.environ["GOOGLE_CLOUD_PROJECT"], "example-project")
            self.assertNotIn("stored-only-in-temp", str(ROOT))
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["private_key"], info["private_key"])
            self.assertFalse(str(path).startswith(str(ROOT)))
        self.assertFalse(path.exists())
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", os.environ)
        self.assertNotIn("GOOGLE_CLOUD_PROJECT", os.environ)

    def test_refuses_to_overwrite_live_snapshot_files(self):
        for name in [
            "data.js",
            "refresh.js",
            "chunks",
            "index.html",
            "delta-2026-09-03.js",
            ".",
        ]:
            with self.assertRaises(refresh.RefreshError):
                refresh.assert_safe_output(ROOT / name)
        allowed = refresh.assert_safe_output(ROOT / "snapshot-refresh")
        self.assertEqual(allowed, (ROOT / "snapshot-refresh").resolve())
        with tempfile.TemporaryDirectory() as tmp:
            outside = refresh.assert_safe_output(Path(tmp) / "ga4")
            self.assertEqual(outside, Path(tmp).resolve() / "ga4")

    def test_snapshot_shape_matches_dashboard_records(self):
        collected = {"series": {}, "sources": {}, "drivers": {}, "content": {}, "hosts": {}}
        metrics = [10, 8, 25, 12.5]
        catalog = refresh.load_catalog()
        refresh.accumulate(
            collected,
            "259299200",
            "daily",
            [row(["20260102", "shopuk.lkk.com"], metrics)],
            catalog,
        )
        refresh.accumulate(
            collected,
            "259299200",
            "source",
            [row(["20260102", "shopuk.lkk.com", "google / organic"], metrics)],
            catalog,
        )
        refresh.accumulate(
            collected,
            "259299200",
            "country",
            [row(["20260102", "shopuk.lkk.com", "China"], metrics)],
            catalog,
        )
        refresh.accumulate(
            collected,
            "259299200",
            "device",
            [row(["20260102", "shopuk.lkk.com", "desktop"], metrics)],
            catalog,
        )
        refresh.accumulate(
            collected,
            "100",
            "content",
            [row(["20260102", "HK.LKK.com", "/en/recipe/char-siu", "Char Siu"], metrics)],
            catalog,
        )
        snapshot = refresh.finalize_snapshot(
            collected,
            start="2026-01-01",
            end="2026-01-02",
            properties=[{"propertyId": "259299200", "displayName": "LKK - Shopify - GA4"}],
            catalog=catalog,
            refreshed_at="2026-11-05T12:00:00+08:00",
        )
        self.assertEqual(snapshot["mcpServer"], "analytics-mcp")
        self.assertEqual(snapshot["timezone"], "Asia/Hong_Kong")
        self.assertEqual(snapshot["refreshedAt"], "2026-11-05T04:00:00Z")
        self.assertEqual(snapshot["series"]["gawa:259299200"][0]["sessions"], 10)
        self.assertEqual(snapshot["series"]["gawa:259299200"][0]["views"], 25)
        self.assertEqual(snapshot["duration"]["gawa:259299200"], [["2026-01-02", 12.5]])
        source = snapshot["sources"]["gawa:259299200"]
        self.assertEqual(source["sources"], ["google / organic"])
        # day index 1, source index 0, sessions, users, views, duration total
        self.assertEqual(source["records"][0][:5], [1, 0, 10, 8, 25])
        self.assertAlmostEqual(source["records"][0][5], 125.0)
        country = snapshot["drivers"]["gawa:259299200"]["country"]
        self.assertEqual(country["labels"], ["China"])
        self.assertEqual(country["records"][0], [1, 0, 10, 8, 25])
        self.assertEqual(len(country["records"][0]), 5)
        content = snapshot["content"]["hk.lkk.com"]["recipe"]
        self.assertEqual(content["paths"], [["/en/recipe/char-siu", "Char Siu"]])
        self.assertEqual(content["records"][0][4], 25)
        labels = {domain["label"] for domain in snapshot["domains"]}
        self.assertIn("United Kingdom — Shopify", labels)
        self.assertIn("Hong Kong", labels)

    def test_default_window_is_this_year_through_yesterday(self):
        start, end = refresh.default_dates(dt.date(2026, 11, 5))
        self.assertEqual(start, "2026-01-01")
        self.assertEqual(end, "2026-11-04")
        self.assertEqual(refresh.resolve_dates("", ""), refresh.default_dates())
        with self.assertRaises(refresh.RefreshError):
            refresh.resolve_dates("2026-02-01", "2026-01-01")

    def test_long_report_window_splits_without_changing_a_single_day(self):
        left, right = refresh.split_window("2026-01-01", "2026-11-04")
        self.assertEqual(left, ("2026-01-01", "2026-06-03"))
        self.assertEqual(right, ("2026-06-04", "2026-11-04"))
        self.assertIsNone(refresh.split_window("2026-11-04", "2026-11-04"))

    def test_property_filter(self):
        summaries = [
            {
                "property_summaries": [
                    {"property": "properties/11", "display_name": "Hong Kong"},
                    {"property": "properties/22", "display_name": "Japan"},
                ]
            }
        ]
        chosen = refresh.properties_from_summaries(summaries, {"22"})
        self.assertEqual(chosen, [{"propertyId": "22", "displayName": "Japan"}])
        with self.assertRaises(refresh.RefreshError):
            refresh.properties_from_summaries(summaries, {"99"})

    def test_workflow_publishes_live_snapshot_without_credentials(self):
        workflow = (ROOT / ".github" / "workflows" / "refresh-ga4-snapshot.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn("0 0 * * *", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("group: ga4-snapshot-refresh", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("GA4_SERVICE_ACCOUNT_JSON: ${{ secrets.GA4_SERVICE_ACCOUNT_JSON }}", workflow)
        self.assertIn("--publish-live", workflow)
        self.assertIn("git commit", workflow)
        self.assertIn("git push origin HEAD:main", workflow)
        self.assertNotIn("private_key", workflow)
        self.assertNotIn("BEGIN PRIVATE", workflow)
        self.assertIn("snapshot-refresh/ga4-snapshot.json", workflow)
        requirements = (ROOT / "scripts" / "requirements-ga4-refresh.txt").read_text(encoding="utf-8")
        self.assertIn("analytics-mcp==1.0.0", requirements)

    def test_merge_keeps_markets_the_pull_cannot_read(self):
        catalog = {
            "hk.lkk.com": {"key": "hk.lkk.com", "host": "hk.lkk.com", "label": "Hong Kong"},
            "usa.lkk.com": {"key": "usa.lkk.com", "host": "usa.lkk.com", "label": "United States"},
        }
        published = {
            "data": {
                "refreshedAt": "2026-09-04T10:03:00+08:00",
                "timezone": "Asia/Hong_Kong",
                "dataStart": "2026-01-01",
                "dataEnd": "2026-09-03",
                "domains": [
                    {"host": "hk.lkk.com", "label": "Hong Kong"},
                    {"host": "usa.lkk.com", "label": "United States"},
                ],
                "series": {
                    "hk.lkk.com": [{"date": "2026-01-01", "sessions": 1, "users": 1, "views": 1}],
                    "usa.lkk.com": [{"date": "2026-09-03", "sessions": 9, "users": 8, "views": 7}],
                },
                "content": {
                    "usa.lkk.com": {
                        "product": {"paths": [["/en/products/x", "X"]], "records": [[245, 0, 4]]}
                    }
                },
            },
            "duration": {"usa.lkk.com": [["2026-09-03", 12.5]], "hk.lkk.com": [["2026-01-01", 3]]},
            "sources": {
                "hk.lkk.com": {"sources": ["old / cpc"], "records": [[0, 0, 1, 1, 1, 0]]},
                "usa.lkk.com": {"sources": ["google / organic"], "records": [[245, 0, 9, 8, 7, 20]]},
            },
            "drivers": {
                "usa.lkk.com": {"country": {"labels": ["China"], "records": [[245, 0, 3]]}},
            },
            "botAdjustments": {
                "hk.lkk.com": {"2026-01-01": [1, 1, 0, 0]},
                "usa.lkk.com": {"2026-09-03": [2, 2, 1, 0]},
            },
        }
        snapshot = {
            "refreshedAt": "2026-11-05T15:26:31+08:00",
            "timezone": "Asia/Hong_Kong",
            "dataStart": "2026-01-01",
            "dataEnd": "2026-11-04",
            "requestedStart": "2026-01-01",
            "requestedEnd": "2026-11-04",
            "domains": [
                {"host": "hk.lkk.com", "label": "Hong Kong"},
                {"host": "spam.example", "label": "spam.example"},
            ],
            "series": {
                "hk.lkk.com": [
                    {"date": "2026-01-01", "sessions": 10, "users": 8, "views": 12},
                    {"date": "2026-11-04", "sessions": 4, "users": 3, "views": 5},
                ],
                "spam.example": [{"date": "2026-08-13", "sessions": 99, "users": 99, "views": 99}],
            },
            "duration": {"hk.lkk.com": [["2026-01-01", 8.5], ["2026-11-04", 9]]},
            "sources": {
                "hk.lkk.com": {"sources": ["google / organic"], "records": [[0, 0, 10, 8, 12, 85], [307, 0, 4, 3, 5, 36]]}
            },
            "drivers": {
                "hk.lkk.com": {"country": {"labels": ["Hong Kong"], "records": [[0, 0, 10, 8, 12]]}}
            },
            "content": {
                "hk.lkk.com": {
                    "product": {
                        "paths": [[f"/en/products/{i}", f"P{i}"] for i in range(40)],
                        "records": [[0, i, 1, 1, 40 - i] for i in range(40)],
                    }
                }
            },
        }
        merged = refresh.merge_live(published, snapshot, catalog)
        self.assertEqual(merged["data"]["refreshedAt"], "2026-11-05T07:26:31Z")
        self.assertEqual([refresh.domain_key(d) for d in merged["data"]["domains"]], ["hk.lkk.com", "usa.lkk.com"])
        self.assertEqual(merged["data"]["series"]["hk.lkk.com"][0]["sessions"], 10)
        self.assertEqual(merged["data"]["series"]["hk.lkk.com"][-1]["date"], "2026-11-04")
        self.assertEqual(merged["data"]["series"]["usa.lkk.com"][0]["sessions"], 9)
        self.assertNotIn("spam.example", merged["data"]["series"])
        self.assertEqual(merged["data"]["dataEnd"], "2026-11-04")
        self.assertIn("usa.lkk.com", merged["sources"])
        self.assertEqual(merged["sources"]["hk.lkk.com"]["sources"], ["google / organic"])
        self.assertNotIn("hk.lkk.com", merged["botAdjustments"])
        self.assertIn("usa.lkk.com", merged["botAdjustments"])
        self.assertLessEqual(len(merged["data"]["content"]["hk.lkk.com"]["product"]["paths"]), 20)
        self.assertEqual(merged["data"]["content"]["usa.lkk.com"]["product"]["records"], [[245, 0, 4]])
        with self.assertRaises(refresh.RefreshError):
            refresh.merge_live(published, {**snapshot, "series": {"spam.example": snapshot["series"]["spam.example"]}}, catalog)

    def test_publish_refuses_credential_material_and_live_json_path(self):
        with self.assertRaises(refresh.RefreshError):
            refresh.assert_no_credentials({"private_key": "secret"})
        with self.assertRaises(refresh.RefreshError):
            refresh.assert_snapshot_usable({"series": {}})
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp)
            with self.assertRaises(refresh.RefreshError):
                refresh._safe_repo_js(ROOT, "../ga4-mcp-secret.js")
            self.assertFalse((outside / "ga4-mcp-secret.js").exists())

    def test_live_writer_roundtrip_preserves_dashboard_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "chunks").mkdir()
            index = """<!doctype html><script src="./data.js?v=old"></script>
<script src="./duration.js?v=old"></script>
<script src="./extensions.js?v=old"></script>
<script src="./magnetic.js?v=old"></script>
<script src="./refresh.js?v=old"></script>
<script src="./sources.js?v=old"></script>
<script src="./drivers-complete.js?v=old"></script>
<script src="./drivers-country-extra.js?v=old"></script>
<script src="./delta-2026-09-03.js?v=old"></script>
"""
            (repo / "index.html").write_text(index, encoding="utf-8")
            published = {
                "data": {
                    "refreshedAt": "2026-09-04T10:03:00+08:00",
                    "timezone": "Asia/Hong_Kong",
                    "dataStart": "2026-01-01",
                    "dataEnd": "2026-09-03",
                    "domains": [
                        {"host": "hk.lkk.com", "label": "Hong Kong"},
                        {"key": "gawa:1", "host": "uk.lkk.com", "label": "United Kingdom"},
                    ],
                    "series": {
                        "hk.lkk.com": [{"date": "2026-01-01", "sessions": 1, "users": 1, "views": 2}],
                        "gawa:1": [{"date": "2026-09-03", "sessions": 5, "users": 4, "views": 6}],
                    },
                    "content": {},
                },
                "duration": {"gawa:1": [["2026-09-03", 10]]},
                "sources": {
                    "hk.lkk.com": {"sources": ["old"], "records": [[0, 0, 1, 1, 2, 0]]},
                    "gawa:1": {"sources": ["direct"], "records": [[245, 0, 5, 4, 6, 50]]},
                },
                "drivers": {"gawa:1": {"device": {"labels": ["desktop"], "records": [[245, 0, 5]]}}},
                "botAdjustments": {"gawa:1": {"2026-09-03": [1, 1, 0, 0]}},
            }
            snapshot = {
                "refreshedAt": "2026-11-05T15:26:31+08:00",
                "timezone": "Asia/Hong_Kong",
                "dataStart": "2026-01-01",
                "dataEnd": "2026-01-02",
                "requestedStart": "2026-01-01",
                "requestedEnd": "2026-11-04",
                "series": {
                    "hk.lkk.com": [
                        {"date": "2026-01-01", "sessions": 3, "users": 2, "views": 4},
                        {"date": "2026-11-04", "sessions": 8, "users": 7, "views": 9},
                    ]
                },
                "duration": {"hk.lkk.com": [["2026-11-04", 11.5]]},
                "sources": {"hk.lkk.com": {"sources": ["google / organic"], "records": [[307, 0, 8, 7, 9, 92]]}},
                "drivers": {"hk.lkk.com": {"country": {"labels": ["China"], "records": [[307, 0, 8, 7, 9]]}}},
                "content": {"hk.lkk.com": {"recipe": {"paths": [["/en/recipe/x", "X"]], "records": [[307, 0, 1, 1, 2]]}}},
                "domains": [{"host": "hk.lkk.com", "label": "Hong Kong"}],
            }
            catalog = {"hk.lkk.com": {"label": "Hong Kong"}, "gawa:1": {"label": "United Kingdom"}}
            bundle = refresh.merge_live(published, snapshot, catalog)
            # Seed the temp repo with the previous published files, then publish over them.
            refresh.write_live_snapshot(repo, published | {"refreshedKeys": []}, snapshot)
            # write_live of the previous bundle used snapshot's cache token; rewrite via publish path.
            refresh.write_live_snapshot(repo, bundle, snapshot)
            refresh.verify_published(repo, bundle)
            text = (repo / "index.html").read_text(encoding="utf-8")
            self.assertIn("./data.js?v=202611051526-ga4", text)
            self.assertIn("./delta-2026-09-03.js", text)
            self.assertNotIn("private_key", (repo / "data.js").read_text(encoding="utf-8"))
            loaded = refresh.load_published_snapshot(repo)
            self.assertEqual(loaded["data"]["series"]["gawa:1"][0]["sessions"], 5)
            self.assertEqual(loaded["data"]["series"]["hk.lkk.com"][-1]["sessions"], 8)
            self.assertEqual(loaded["data"]["refreshedAt"], "2026-11-05T07:26:31Z")

    def test_refresh_instant_is_utc_and_dashboard_reads_it(self):
        self.assertEqual(refresh.as_utc_iso("2026-11-05T15:26:31+08:00"), "2026-11-05T07:26:31Z")
        self.assertEqual(refresh.as_utc_iso("2026-11-05T08:00:00Z"), "2026-11-05T08:00:00Z")
        self.assertEqual(refresh.as_utc_iso("2026-11-05T08:00:00+00:00"), "2026-11-05T08:00:00Z")
        self.assertEqual(refresh.as_utc_iso(""), "")
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        title_at = html.find('id="chart-title"')
        refresh_at = html.find('id="refreshed-at"')
        stamp_at = html.find('id="stamp"')
        self.assertGreater(title_at, 0)
        self.assertGreater(refresh_at, title_at)
        self.assertLess(refresh_at, stamp_at)
        self.assertIn("D.refreshedAt", html)
        self.assertIn("refreshedAtUtc", html)
        self.assertIn('refreshClock(instant,"Asia/Hong_Kong")', html)
        self.assertIn(" local · ", html)
        self.assertIn(" Hong Kong", html)
        self.assertNotIn("5 Nov 2026, 3:26 PM", html)
        self.assertNotIn("Date.now(", html)


if __name__ == "__main__":
    unittest.main()

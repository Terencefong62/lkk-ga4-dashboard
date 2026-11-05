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

    def test_workflow_uses_secret_and_does_not_publish_live_js(self):
        workflow = (ROOT / ".github" / "workflows" / "refresh-ga4-snapshot.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("GA4_SERVICE_ACCOUNT_JSON: ${{ secrets.GA4_SERVICE_ACCOUNT_JSON }}", workflow)
        requirements = (ROOT / "scripts" / "requirements-ga4-refresh.txt").read_text(encoding="utf-8")
        self.assertIn("analytics-mcp==1.0.0", requirements)
        self.assertIn("snapshot-refresh", workflow)
        self.assertNotIn("private_key", workflow)
        self.assertNotIn("git commit", workflow)
        self.assertNotIn("git push", workflow)
        self.assertIn("snapshot-refresh/ga4-snapshot.json", workflow)


if __name__ == "__main__":
    unittest.main()

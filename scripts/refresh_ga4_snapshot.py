#!/usr/bin/env python3
"""Refresh a GA4 dashboard snapshot with Google's official Analytics MCP server.

The official server is the PyPI package and console script ``analytics-mcp``
(entrypoint also published as ``google-analytics-mcp``) from
https://github.com/googleanalytics/google-analytics-mcp. This job calls that
server's read-only tools (``get_account_summaries`` and ``run_report``).

Credentials are read only from the ``GA4_SERVICE_ACCOUNT_JSON`` environment
variable, which must be the service-account JSON itself. The script never
reads a key file from the repo and never writes the key into the snapshot.

Refreshed data is written under ``snapshot-refresh/`` as JSON. Pass
``--publish-live`` to also rewrite the dashboard JavaScript that index.html
loads (``data.js``, ``duration.js``, ``sources.js``, ``drivers-complete.js``,
the chunk files those loaders pull in, and the patch files that must not
re-apply an older export). The service-account JSON is never written.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

OFFICIAL_MCP_SERVER = "analytics-mcp"
SECRET_ENV = "GA4_SERVICE_ACCOUNT_JSON"
TIMEZONE = "Asia/Hong_Kong"
HKT = ZoneInfo(TIMEZONE)

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = Path(__file__).resolve().parent / "ga4_domain_catalog.json"
DEFAULT_OUT = REPO_ROOT / "snapshot-refresh"

# The JSON writer must not drop a snapshot onto these paths. --publish-live
# rewrites the same dashboard files on purpose, through write_live_snapshot.
LIVE_SNAPSHOT_FILES = {
    "data.js",
    "duration.js",
    "extensions.js",
    "magnetic.js",
    "refresh.js",
    "sources.js",
    "drivers-complete.js",
    "drivers-country-extra.js",
    "index.html",
}

# Product and recipe cards show five rows. Keep each month's top paths, plus
# the overall top paths, so a month or a year still has a real top five
# without shipping every URL the API returns.
CONTENT_MONTHLY_CAP = 20
CONTENT_OVERALL_CAP = 20
# Chunk external scripts the way the published site already does, once the
# assignment is larger than a few hundred kilobytes.
CHUNK_CHARS = 400_000
DIRECT_JS_LIMIT = 300_000

CHUNKED_GLOBALS = {
    "data.js": ("__LKK_DATA_PARTS", "data"),
    "sources.js": ("__LKK_SOURCES_PARTS", "sources"),
    "drivers-complete.js": ("__LKK_DRIVERS_PARTS", "drivers-complete"),
}

NOOP_JS = (
    "/* This patch file is loaded by index.html. The refresh job folds its\n"
    "   data into the canonical snapshot files and leaves this script inert\n"
    "   so an older export cannot overwrite the Google Analytics Data API pull.\n"
    "*/\n"
)

DAILY_METRICS = [
    "sessions",
    "activeUsers",
    "screenPageViews",
    "averageSessionDuration",
]
PAGE_SIZE = 100_000
# analytics-mcp's Data API client times out at 60 seconds. A busy property
# can miss that on a full-year page, so those calls retry and then split.
REPORT_ATTEMPTS = 2
RETRYABLE_API_ERRORS = {
    "DeadlineExceeded",
    "ServiceUnavailable",
    "InternalServerError",
    "TooManyRequests",
    "ResourceExhausted",
    "Aborted",
}


class RefreshError(SystemExit):
    """Failure that is safe to show (no credential material)."""


def parse_service_account(raw: str | None) -> dict:
    """Parse the service-account JSON from the environment value only."""
    text = (raw or "").strip()
    if not text:
        raise RefreshError(
            f"Set {SECRET_ENV} to the Google service account JSON. "
            "Credentials are read only from that environment variable."
        )
    if not text.startswith("{"):
        raise RefreshError(
            f"{SECRET_ENV} must be the JSON key itself, not a file path."
        )
    try:
        info = json.loads(text)
    except json.JSONDecodeError:
        raise RefreshError(f"{SECRET_ENV} is not valid JSON.") from None
    if not isinstance(info, dict):
        raise RefreshError(f"{SECRET_ENV} must be a JSON object.")
    if (
        info.get("type") != "service_account"
        or not isinstance(info.get("private_key"), str)
        or not info.get("private_key")
        or not isinstance(info.get("client_email"), str)
        or not info.get("client_email")
    ):
        raise RefreshError(
            f"{SECRET_ENV} is not a Google service account key "
            "(expected type, client_email, and private_key)."
        )
    return info


class ServiceAccountCredentials:
    """Materialize the env JSON as a temp ADC file, then delete it."""

    def __init__(self, info: dict):
        self.info = info
        self.path: Path | None = None
        self._previous_credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        self._previous_project = os.environ.get("GOOGLE_CLOUD_PROJECT")

    def __enter__(self) -> ServiceAccountCredentials:
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="ga4-sa-",
            suffix=".json",
            delete=False,
        )
        try:
            json.dump(self.info, handle)
            handle.flush()
        finally:
            handle.close()
        self.path = Path(handle.name)
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(self.path)
        project = self.info.get("project_id")
        if isinstance(project, str) and project:
            os.environ["GOOGLE_CLOUD_PROJECT"] = project
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            self.path = None
        if self._previous_credentials is None:
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        else:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self._previous_credentials
        if self._previous_project is None:
            os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        else:
            os.environ["GOOGLE_CLOUD_PROJECT"] = self._previous_project


def assert_safe_output(out_dir: Path, repo_root: Path = REPO_ROOT) -> Path:
    """Allow a new snapshot directory and reject live dashboard paths."""
    out = out_dir.expanduser().resolve()
    repo = repo_root.resolve()
    name = out.name
    if name in LIVE_SNAPSHOT_FILES or (name.startswith("delta-") and out.suffix == ".js"):
        raise RefreshError(f"Refusing to overwrite live snapshot file {out.name}.")
    try:
        relative = out.relative_to(repo)
    except ValueError:
        return out
    if not relative.parts or relative.parts[0] != "snapshot-refresh":
        raise RefreshError(
            "Refusing to write a refreshed snapshot outside snapshot-refresh/. "
            "Live dashboard JavaScript is left unchanged."
        )
    if any(part in LIVE_SNAPSHOT_FILES or part == "chunks" for part in relative.parts):
        raise RefreshError("Refusing to overwrite live snapshot JavaScript.")
    return out


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, dict]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    catalog = {}
    for entry in data:
        key = str(entry.get("key") or entry.get("host") or "").strip()
        if key:
            catalog[key] = entry
        property_id = str(entry.get("propertyId") or "").strip()
        if property_id:
            catalog[f"gawa:{property_id}"] = entry
        host = str(entry.get("host") or "").strip().lower()
        if host and key == host:
            catalog[host] = entry
    return catalog


def default_dates(today: dt.date | None = None) -> tuple[str, str]:
    current = today or dt.datetime.now(HKT).date()
    start = dt.date(current.year, 1, 1)
    end = current - dt.timedelta(days=1)
    if end < start:
        end = start
    return start.isoformat(), end.isoformat()


def parse_iso_date(value: str, flag: str) -> str:
    text = value.strip()
    if not text:
        raise RefreshError(f"{flag} is empty.")
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        raise RefreshError(f"{flag} must be YYYY-MM-DD.") from None


def resolve_dates(start: str | None, end: str | None) -> tuple[str, str]:
    default_start, default_end = default_dates()
    start_date = parse_iso_date(start, "start date") if start and start.strip() else default_start
    end_date = parse_iso_date(end, "end date") if end and end.strip() else default_end
    if end_date < start_date:
        raise RefreshError("End date is before the start date.")
    return start_date, end_date


def iso_from_ga4(value: str) -> str:
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return parse_iso_date(text, "GA4 date")


def series_key(host: str, property_id: str, catalog: dict[str, dict]) -> str:
    """Keep property-keyed markets separate and host-keyed markets on the host."""
    gawa = f"gawa:{property_id}"
    if gawa in catalog:
        return gawa
    cleaned = (host or "").strip().lower()
    if cleaned and cleaned != "(not set)":
        return cleaned
    return gawa


def content_kind(path: str) -> str | None:
    lowered = path.lower()
    if "/recipe" in lowered:
        return "recipe"
    if "/product" in lowered:
        return "product"
    return None


def _metric(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def row_values(row: dict) -> tuple[list[str], list[float]]:
    dimensions = row.get("dimension_values") or row.get("dimensionValues") or []
    metrics = row.get("metric_values") or row.get("metricValues") or []
    dim_values = [str(item.get("value", "")) for item in dimensions]
    metric_values = [_metric(item.get("value", "0")) for item in metrics]
    return dim_values, metric_values


def new_totals() -> dict:
    return {"sessions": 0, "users": 0, "views": 0, "duration": 0.0}


def add_totals(bucket: dict, sessions: float, users: float, views: float, average_duration: float) -> None:
    session_count = int(round(sessions))
    bucket["sessions"] += session_count
    bucket["users"] += int(round(users))
    bucket["views"] += int(round(views))
    bucket["duration"] += float(average_duration) * session_count


def property_id_from_name(name: str) -> str:
    text = str(name or "").strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    if not text.isdigit():
        raise RefreshError(f"Unexpected GA4 property resource {name!r}.")
    return text


def properties_from_summaries(summaries: list[dict], only: set[str] | None) -> list[dict]:
    found = []
    seen = set()
    for account in summaries or []:
        props = account.get("property_summaries") or account.get("propertySummaries") or []
        for prop in props:
            resource = prop.get("property") or ""
            property_id = property_id_from_name(resource)
            if only and property_id not in only:
                continue
            if property_id in seen:
                continue
            seen.add(property_id)
            found.append(
                {
                    "propertyId": property_id,
                    "displayName": prop.get("display_name") or prop.get("displayName") or property_id,
                }
            )
    if only:
        missing = sorted(only - seen)
        if missing:
            raise RefreshError(
                "The service account cannot see these property IDs: " + ", ".join(missing)
            )
    if not found:
        raise RefreshError("No GA4 properties were visible to the service account.")
    return found


def _bucket(store: dict, *keys) -> dict:
    node = store
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    return node.setdefault(keys[-1], new_totals())


def accumulate(snapshot: dict, property_id: str, report: str, rows: list[dict], catalog: dict[str, dict]) -> None:
    for row in rows:
        dims, metrics = row_values(row)
        if len(metrics) < 4 or len(dims) < 2:
            continue
        date = iso_from_ga4(dims[0])
        key = series_key(dims[1], property_id, catalog)
        sessions, users, views, average = metrics[0], metrics[1], metrics[2], metrics[3]
        if report == "daily":
            add_totals(_bucket(snapshot["series"], key, date), sessions, users, views, average)
        elif report == "source" and len(dims) >= 3:
            label = dims[2].strip() or "(not set)"
            add_totals(_bucket(snapshot["sources"], key, date, label), sessions, users, views, average)
        elif report in {"country", "device", "campaign"} and len(dims) >= 3:
            label = dims[2].strip() or "(not set)"
            add_totals(_bucket(snapshot["drivers"], key, report, date, label), sessions, users, views, average)
        elif report == "content" and len(dims) >= 4:
            kind = content_kind(dims[2])
            if not kind:
                continue
            title = dims[3].strip() or dims[2]
            add_totals(
                _bucket(snapshot["content"], key, kind, date, (dims[2], title)),
                sessions,
                users,
                views,
                average,
            )
        snapshot["hosts"].setdefault(key, set()).add(property_id)


def as_utc_iso(value: object) -> str:
    """Normalize a refresh instant to UTC ISO-8601. Empty or unparseable stays empty."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration_seconds(bucket: dict) -> float:
    if not bucket["sessions"]:
        return 0.0
    return round(bucket["duration"] / bucket["sessions"], 4)


def _record_tail(bucket: dict) -> list[float]:
    return [bucket["sessions"], bucket["users"], bucket["views"], round(bucket["duration"], 4)]


def finalize_snapshot(
    collected: dict,
    *,
    start: str,
    end: str,
    properties: list[dict],
    catalog: dict[str, dict],
    refreshed_at: str,
) -> dict:
    start_date = dt.date.fromisoformat(start)
    series_out = {}
    duration_out = {}
    for key, days in sorted(collected["series"].items()):
        points = []
        durations = []
        for date in sorted(days):
            bucket = days[date]
            if bucket["sessions"] == 0 and bucket["users"] == 0 and bucket["views"] == 0:
                continue
            points.append(
                {
                    "date": date,
                    "sessions": bucket["sessions"],
                    "users": bucket["users"],
                    "views": bucket["views"],
                }
            )
            durations.append([date, _duration_seconds(bucket)])
        if points:
            series_out[key] = points
            duration_out[key] = durations

    def day_index(date: str) -> int:
        return (dt.date.fromisoformat(date) - start_date).days

    sources_out = {}
    for key, days in collected["sources"].items():
        labels: list[str] = []
        index = {}
        records = []
        for date in sorted(days):
            for label, bucket in sorted(days[date].items()):
                if bucket["sessions"] == 0 and bucket["views"] == 0:
                    continue
                if label not in index:
                    index[label] = len(labels)
                    labels.append(label)
                records.append([day_index(date), index[label], *_record_tail(bucket)])
        if records:
            sources_out[key] = {"sources": labels, "records": records}

    drivers_out = {}
    for key, kinds in collected["drivers"].items():
        root = {}
        for kind, days in kinds.items():
            labels = []
            index = {}
            records = []
            for date in sorted(days):
                for label, bucket in sorted(days[date].items()):
                    if bucket["sessions"] == 0 and bucket["views"] == 0:
                        continue
                    if label not in index:
                        index[label] = len(labels)
                        labels.append(label)
                    # views sit at index 4 so the dashboard donuts can use them.
                    records.append(
                        [day_index(date), index[label], bucket["sessions"], bucket["users"], bucket["views"]]
                    )
            if records:
                root[kind] = {"labels": labels, "records": records}
        if root:
            drivers_out[key] = root

    content_out = {}
    for key, kinds in collected["content"].items():
        root = {}
        for kind, days in kinds.items():
            paths = []
            index = {}
            records = []
            for date in sorted(days):
                for (path, title), bucket in sorted(days[date].items()):
                    if bucket["views"] == 0 and bucket["sessions"] == 0:
                        continue
                    identity = (path, title)
                    if identity not in index:
                        index[identity] = len(paths)
                        paths.append([path, title])
                    records.append(
                        [day_index(date), index[identity], bucket["sessions"], bucket["users"], bucket["views"]]
                    )
            if records:
                root[kind] = {"paths": paths, "records": records}
        if root:
            content_out[key] = root

    keys = set(series_out) | set(collected["hosts"])
    domains = []
    for key in sorted(keys, key=lambda item: (catalog.get(item, {}).get("label") or item).lower()):
        known = catalog.get(key, {})
        host = known.get("host") or (key if not key.startswith("gawa:") else "No hostname recorded")
        label = known.get("label") or (host if host != "No hostname recorded" else key)
        domain = {"host": host, "label": label}
        if key != host:
            domain["key"] = key
        property_ids = sorted(collected["hosts"].get(key, set()))
        if len(property_ids) == 1:
            domain["propertyId"] = property_ids[0]
        elif known.get("propertyId"):
            domain["propertyId"] = known["propertyId"]
        domains.append(domain)

    present_dates = [point["date"] for points in series_out.values() for point in points]
    data_start = min(present_dates) if present_dates else start
    data_end = max(present_dates) if present_dates else end
    return {
        "mcpServer": OFFICIAL_MCP_SERVER,
        "refreshedAt": as_utc_iso(refreshed_at) or refreshed_at,
        "timezone": TIMEZONE,
        "dataStart": data_start,
        "dataEnd": data_end,
        "requestedStart": start,
        "requestedEnd": end,
        "properties": properties,
        "domains": domains,
        "series": series_out,
        "duration": duration_out,
        "sources": sources_out,
        "drivers": drivers_out,
        "content": content_out,
    }


def page_filter() -> dict:
    def contains(value: str) -> dict:
        return {
            "filter": {
                "field_name": "pagePath",
                "string_filter": {
                    "match_type": "CONTAINS",
                    "value": value,
                    "case_sensitive": False,
                },
            }
        }

    return {"or_group": {"expressions": [contains("/product"), contains("/recipe")]}}


def split_window(start: str, end: str) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """Split an inclusive date window in half. A single day cannot be split."""
    start_day = dt.date.fromisoformat(start)
    end_day = dt.date.fromisoformat(end)
    span = (end_day - start_day).days
    if span < 1:
        return None
    mid = start_day + dt.timedelta(days=span // 2)
    right = mid + dt.timedelta(days=1)
    return (start, mid.isoformat()), (right.isoformat(), end)


async def _run_report_page(run_report, property_id: str, **kwargs) -> dict:
    """Call analytics-mcp run_report, retrying transient deadline and 5xx errors."""
    delay = 2
    last_error = "Error"
    for attempt in range(REPORT_ATTEMPTS):
        try:
            report = await run_report(property_id=property_id, **kwargs)
        except Exception as exc:
            last_error = type(exc).__name__
            if last_error not in RETRYABLE_API_ERRORS or attempt + 1 == REPORT_ATTEMPTS:
                raise RefreshError(
                    f"run_report for property {property_id} failed ({last_error})."
                ) from None
            print(f"  retrying property {property_id} after {last_error}", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if not isinstance(report, dict):
            raise RefreshError(f"run_report for property {property_id} returned an unexpected response.")
        return report
    raise RefreshError(f"run_report for property {property_id} failed ({last_error}).")


async def fetch_rows(run_report, property_id: str, *, dimensions, metrics, start: str, end: str, dimension_filter=None) -> list[dict]:
    rows = []
    offset = 0
    while True:
        report = await _run_report_page(
            run_report,
            property_id,
            date_ranges=[{"start_date": start, "end_date": end, "name": "snapshot"}],
            dimensions=dimensions,
            metrics=metrics,
            dimension_filter=dimension_filter,
            limit=PAGE_SIZE,
            offset=offset or None,
            return_property_quota=False,
        )
        batch = report.get("rows") or []
        rows.extend(batch)
        row_count = int(report.get("row_count") or 0)
        offset += len(batch)
        if not batch or offset >= row_count:
            return rows


async def fetch_report_rows(run_report, property_id: str, *, dimensions, metrics, start: str, end: str, dimension_filter=None) -> list[dict]:
    """Read one report, splitting the date window when the API deadline wins."""
    try:
        return await fetch_rows(
            run_report,
            property_id,
            dimensions=dimensions,
            metrics=metrics,
            start=start,
            end=end,
            dimension_filter=dimension_filter,
        )
    except RefreshError as exc:
        if "DeadlineExceeded" not in str(exc):
            raise
        parts = split_window(start, end)
        if parts is None:
            raise
        print(f"  property {property_id}: splitting {start} to {end} after the API deadline", flush=True)
        left, right = parts
        rows = []
        for chunk_start, chunk_end in (left, right):
            rows.extend(
                await fetch_report_rows(
                    run_report,
                    property_id,
                    dimensions=dimensions,
                    metrics=metrics,
                    start=chunk_start,
                    end=chunk_end,
                    dimension_filter=dimension_filter,
                )
            )
        return rows


async def refresh(info: dict, start: str, end: str, only: set[str] | None, catalog: dict[str, dict]) -> dict:
    from analytics_mcp.tools.admin.info import get_account_summaries
    from analytics_mcp.tools.reporting.core import run_report

    summaries = await get_account_summaries()
    properties = properties_from_summaries(summaries, only)
    print(
        f"Using {OFFICIAL_MCP_SERVER} as {info['client_email']} "
        f"for {len(properties)} propert{'y' if len(properties) == 1 else 'ies'} "
        f"({start} to {end}).",
        flush=True,
    )
    collected = {"series": {}, "sources": {}, "drivers": {}, "content": {}, "hosts": {}}
    reports = [
        ("daily", ["date", "hostName"], DAILY_METRICS, None),
        ("source", ["date", "hostName", "sessionSourceMedium"], DAILY_METRICS, None),
        ("country", ["date", "hostName", "country"], DAILY_METRICS, None),
        ("device", ["date", "hostName", "deviceCategory"], DAILY_METRICS, None),
        ("campaign", ["date", "hostName", "sessionCampaignName"], DAILY_METRICS, None),
        ("content", ["date", "hostName", "pagePath", "pageTitle"], DAILY_METRICS, page_filter()),
    ]
    for prop in properties:
        property_id = prop["propertyId"]
        print(f"Property {property_id} ({prop['displayName']})", flush=True)
        for name, dimensions, metrics, dimension_filter in reports:
            rows = await fetch_report_rows(
                run_report,
                property_id,
                dimensions=dimensions,
                metrics=metrics,
                start=start,
                end=end,
                dimension_filter=dimension_filter,
            )
            accumulate(collected, property_id, name, rows, catalog)
            print(f"  {name}: {len(rows)} rows", flush=True)
    refreshed_at = as_utc_iso(dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    return finalize_snapshot(
        collected,
        start=start,
        end=end,
        properties=properties,
        catalog=catalog,
        refreshed_at=refreshed_at,
    )


def assert_no_credentials(payload: object) -> None:
    """Reject snapshot documents that smuggle a service-account key."""
    if isinstance(payload, (dict, list)):
        blob = json.dumps(payload)
    else:
        blob = str(payload)
    if "private_key" in blob or "BEGIN PRIVATE KEY" in blob or "BEGIN RSA PRIVATE KEY" in blob:
        raise RefreshError("Refusing to write a snapshot that contains credential material.")


def assert_snapshot_usable(snapshot: dict) -> None:
    series = snapshot.get("series") or {}
    if not isinstance(series, dict) or not series:
        raise RefreshError("GA4 snapshot has no series. Refusing to continue.")
    sessions = 0
    for points in series.values():
        if not isinstance(points, list):
            continue
        for point in points:
            if isinstance(point, dict):
                sessions += int(point.get("sessions") or 0)
    if sessions <= 0:
        raise RefreshError("GA4 snapshot series are empty. Refusing to continue.")


def js_json(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def domain_key(domain: dict) -> str:
    return str(domain.get("key") or domain.get("host") or "")


def catalog_keys(catalog: dict[str, dict]) -> set[str]:
    return set(catalog)


def _parse_day(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def snapshot_window(snapshot: dict) -> tuple[str, str]:
    start = str(snapshot.get("requestedStart") or snapshot.get("dataStart") or "")
    end = str(snapshot.get("requestedEnd") or snapshot.get("dataEnd") or "")
    if not start or not end:
        raise RefreshError("GA4 snapshot is missing its date window.")
    _parse_day(start)
    _parse_day(end)
    return start, end


def _dated_metrics(body: dict, base: dt.date, label_field: str) -> dict[str, dict]:
    labels = body.get(label_field) or []
    grouped: dict[str, dict] = {}
    for record in body.get("records") or []:
        if len(record) < 3:
            continue
        date = (base + dt.timedelta(days=int(record[0]))).isoformat()
        label = labels[int(record[1])]
        if label_field == "paths":
            label = tuple(label)
        tail = list(record[2:])
        bucket = grouped.setdefault(date, {})
        current = bucket.get(label)
        if current is None:
            bucket[label] = tail
        else:
            width = max(len(current), len(tail))
            padded_current = current + [0] * (width - len(current))
            padded_tail = tail + [0] * (width - len(tail))
            bucket[label] = [a + b for a, b in zip(padded_current, padded_tail)]
    return grouped


def _emit_metrics(grouped: dict[str, dict], base: dt.date, label_field: str) -> dict:
    labels: list = []
    index: dict = {}
    records = []
    for date in sorted(grouped):
        day = (_parse_day(date) - base).days
        for label in sorted(grouped[date], key=lambda item: json.dumps(item, ensure_ascii=False)):
            if label not in index:
                index[label] = len(labels)
                labels.append(list(label) if label_field == "paths" else label)
            records.append([day, index[label], *grouped[date][label]])
    lengths = {len(record) for record in records}
    if len(lengths) > 1:
        width = min(lengths)
        records = [record[:width] for record in records]
    return {label_field: labels, "records": records}


def merge_metric_body(
    old: dict | None,
    new: dict | None,
    *,
    old_base: dt.date,
    new_base: dt.date,
    merged_base: dt.date,
    start: str,
    end: str,
    label_field: str,
    replace_all: bool,
) -> dict | None:
    if replace_all:
        source = new or old
        if not source:
            return None
        grouped = _dated_metrics(source, new_base if new else old_base, label_field)
        return _emit_metrics(grouped, merged_base, label_field)
    grouped: dict[str, dict] = {}
    if old:
        grouped.update(_dated_metrics(old, old_base, label_field))
    if new:
        incoming = _dated_metrics(new, new_base, label_field)
        for date in list(grouped):
            if start <= date <= end:
                grouped.pop(date, None)
        grouped.update(incoming)
    if not grouped:
        return None
    return _emit_metrics(grouped, merged_base, label_field)


def merge_series(old_points: list | None, new_points: list | None, start: str, end: str, replace_all: bool) -> list:
    if replace_all:
        return list(new_points or old_points or [])
    by_date = {point["date"]: point for point in (old_points or []) if isinstance(point, dict) and point.get("date")}
    for date in list(by_date):
        if start <= date <= end:
            by_date.pop(date, None)
    for point in new_points or []:
        if isinstance(point, dict) and start <= str(point.get("date")) <= end:
            by_date[point["date"]] = point
    return [by_date[date] for date in sorted(by_date)]


def merge_duration(old_rows: list | None, new_rows: list | None, start: str, end: str, replace_all: bool) -> list:
    if replace_all:
        return list(new_rows or old_rows or [])
    by_date = {row[0]: row for row in (old_rows or []) if isinstance(row, list) and row}
    for date in list(by_date):
        if start <= date <= end:
            by_date.pop(date, None)
    for row in new_rows or []:
        if isinstance(row, list) and row and start <= str(row[0]) <= end:
            by_date[row[0]] = row
    return [by_date[date] for date in sorted(by_date)]


def cap_content_body(body: dict, base: dt.date) -> dict:
    """Keep each month's top paths and the overall top paths."""
    paths = body.get("paths") or []
    records = body.get("records") or []
    overall: dict[int, float] = {}
    monthly: dict[tuple[int, int], dict[int, float]] = {}
    for record in records:
        if len(record) < 3:
            continue
        views = float(record[4] if len(record) >= 5 else record[2])
        path_index = int(record[1])
        overall[path_index] = overall.get(path_index, 0.0) + views
        day = base + dt.timedelta(days=int(record[0]))
        month = monthly.setdefault((day.year, day.month), {})
        month[path_index] = month.get(path_index, 0.0) + views
    chosen = set(sorted(overall, key=lambda index: overall[index], reverse=True)[:CONTENT_OVERALL_CAP])
    for scores in monthly.values():
        chosen.update(sorted(scores, key=lambda index: scores[index], reverse=True)[:CONTENT_MONTHLY_CAP])
    remap: dict[int, int] = {}
    new_paths = []
    for index in sorted(chosen):
        if index < 0 or index >= len(paths):
            continue
        remap[index] = len(new_paths)
        new_paths.append(paths[index])
    new_records = []
    for record in records:
        mapped = remap.get(int(record[1]))
        if mapped is None:
            continue
        new_records.append([record[0], mapped, *record[2:]])
    return {"paths": new_paths, "records": new_records}


def cap_content_root(root: dict | None, base: dt.date) -> dict | None:
    if not root:
        return None
    return {kind: cap_content_body(body, base) for kind, body in root.items() if isinstance(body, dict)}


def trim_adjustments(adjustments: dict, key: str, start: str, end: str) -> None:
    per_key = adjustments.get(key)
    if not isinstance(per_key, dict):
        return
    for date in list(per_key):
        if start <= str(date) <= end:
            per_key.pop(date, None)
    if not per_key:
        adjustments.pop(key, None)


def merge_live(published: dict, snapshot: dict, catalog: dict[str, dict]) -> dict:
    """Overlay catalog markets from a GA4 pull onto the published snapshot.

    Markets the service account cannot read keep their previous series, duration,
    sources, drivers, content, and bot adjustments. Junk hostnames that are not
    in the domain catalog are ignored.
    """
    data = published.get("data") or {}
    if not data.get("domains") or not data.get("series"):
        raise RefreshError("Published dashboard snapshot is missing domains or series.")
    start, end = snapshot_window(snapshot)
    published_start = str(data.get("dataStart") or start)
    published_end = str(data.get("dataEnd") or end)
    merged_start = min(published_start, str(snapshot.get("dataStart") or start), start)
    merged_end = max(published_end, str(snapshot.get("dataEnd") or end), end)
    replace_all = start <= published_start and end >= published_end
    old_base = _parse_day(published_start)
    new_base = _parse_day(start)
    merged_base = _parse_day(merged_start)
    known = catalog_keys(catalog)
    fresh_series = snapshot.get("series") or {}
    refreshed = sorted(key for key in fresh_series if key in known and fresh_series.get(key))
    if not refreshed:
        raise RefreshError(
            "No catalog markets were present in the GA4 snapshot. "
            "Refusing to publish an empty dashboard."
        )

    domains = [dict(domain) for domain in data["domains"]]
    present = {domain_key(domain) for domain in domains}
    snapshot_domains = {domain_key(domain): domain for domain in snapshot.get("domains") or []}
    for key in refreshed:
        if key not in present and key in snapshot_domains:
            domains.append(dict(snapshot_domains[key]))
            present.add(key)

    series = {key: list(points) for key, points in (data.get("series") or {}).items()}
    duration = {key: list(rows) for key, rows in (published.get("duration") or {}).items()}
    sources = {key: value for key, value in (published.get("sources") or {}).items()}
    drivers = {key: value for key, value in (published.get("drivers") or {}).items()}
    content = {key: value for key, value in (data.get("content") or {}).items()}
    adjustments = json.loads(json.dumps(published.get("botAdjustments") or {}))

    for key in refreshed:
        series[key] = merge_series(series.get(key), fresh_series.get(key), start, end, replace_all)
        if not series[key]:
            raise RefreshError(f"Refusing to publish {key} with an empty series.")
        if key in sources and not (snapshot.get("sources") or {}).get(key):
            raise RefreshError(f"{key} has no source breakdown in the GA4 pull.")
        duration[key] = merge_duration(
            duration.get(key),
            (snapshot.get("duration") or {}).get(key),
            start,
            end,
            replace_all,
        )
        merged_source = merge_metric_body(
            sources.get(key),
            (snapshot.get("sources") or {}).get(key),
            old_base=old_base,
            new_base=new_base,
            merged_base=merged_base,
            start=start,
            end=end,
            label_field="sources",
            replace_all=replace_all,
        )
        if merged_source:
            sources[key] = merged_source
        old_driver = drivers.get(key) or {}
        new_driver = (snapshot.get("drivers") or {}).get(key) or {}
        kinds = set(old_driver) | set(new_driver)
        driver_root = {}
        for kind in sorted(kinds):
            merged_kind = merge_metric_body(
                old_driver.get(kind),
                new_driver.get(kind),
                old_base=old_base,
                new_base=new_base,
                merged_base=merged_base,
                start=start,
                end=end,
                label_field="labels",
                replace_all=replace_all,
            )
            if merged_kind:
                driver_root[kind] = merged_kind
        if driver_root:
            drivers[key] = driver_root
        old_content = content.get(key) or {}
        new_content = (snapshot.get("content") or {}).get(key) or {}
        content_root = {}
        for kind in sorted(set(old_content) | set(new_content)):
            merged_kind = merge_metric_body(
                old_content.get(kind),
                new_content.get(kind),
                old_base=old_base,
                new_base=new_base,
                merged_base=merged_base,
                start=start,
                end=end,
                label_field="paths",
                replace_all=replace_all,
            )
            if merged_kind:
                content_root[kind] = merged_kind
        capped = cap_content_root(content_root, merged_base)
        if capped:
            content[key] = capped
        trim_adjustments(adjustments, key, start, end)

    for domain in domains:
        key = domain_key(domain)
        if key not in series or not series[key]:
            raise RefreshError(f"Refusing to publish without series for {key}.")

    merged_data = {
        "refreshedAt": as_utc_iso(snapshot.get("refreshedAt") or data.get("refreshedAt")),
        "timezone": snapshot.get("timezone") or data.get("timezone") or TIMEZONE,
        "dataStart": merged_start,
        "dataEnd": merged_end,
        "domains": domains,
        "series": series,
        "content": content,
    }
    return {
        "data": merged_data,
        "duration": duration,
        "sources": sources,
        "drivers": drivers,
        "botAdjustments": adjustments,
        "refreshedKeys": refreshed,
    }


def load_published_snapshot(repo_root: Path = REPO_ROOT) -> dict:
    """Execute the live script chain index.html loads."""
    script = Path(__file__).resolve().parent / "read_live_snapshot.mjs"
    if not script.is_file():
        raise RefreshError(f"Missing {script.name}.")
    handle = tempfile.NamedTemporaryFile(prefix="ga4-published-", suffix=".json", delete=False)
    handle.close()
    out_path = Path(handle.name)
    try:
        completed = subprocess.run(
            ["node", str(script), str(repo_root), str(out_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RefreshError(f"Could not read the published snapshot. {detail}")
        if completed.stdout.strip():
            print(completed.stdout.strip(), flush=True)
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        out_path.unlink(missing_ok=True)
    assert_no_credentials(payload)
    return payload


def _snapshot_scripts(index_text: str) -> list[str]:
    return re.findall(r'<script src="\./([^"?]+\.js)', index_text)


def _safe_repo_js(repo_root: Path, relative: str) -> Path:
    repo = repo_root.resolve()
    path = (repo / relative).resolve()
    if not path.is_relative_to(repo):
        raise RefreshError(f"Refusing to write outside the repo: {relative}")
    name = path.name.lower()
    if path.suffix != ".js":
        raise RefreshError(f"Refusing to write non-JavaScript snapshot path {relative}.")
    if "service" in name and "account" in name:
        raise RefreshError("Refusing to write a service-account file.")
    if name.startswith("ga4-mcp") or name.startswith("ga4-sa-") or name.endswith(".json"):
        raise RefreshError("Refusing to write a credential or raw snapshot file.")
    return path


def _assignment_js(global_name: str, value: object) -> str:
    return f"window.{global_name}={js_json(value)};\n"


def _write_chunked(repo_root: Path, script_name: str, source: str) -> None:
    parts_name, prefix = CHUNKED_GLOBALS[script_name]
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    chunks = [encoded[index : index + CHUNK_CHARS] for index in range(0, len(encoded), CHUNK_CHARS)] or [""]
    chunk_dir = _safe_repo_js(repo_root, "chunks/keep.js").parent
    chunk_dir.mkdir(parents=True, exist_ok=True)
    for stale in chunk_dir.glob(f"{prefix}-*.js"):
        stale.unlink()
    tags = []
    for index, chunk in enumerate(chunks):
        relative = f"chunks/{prefix}-{index}.js"
        _safe_repo_js(repo_root, relative).write_text(
            f'window.{parts_name}.push("{chunk}");\n',
            encoding="utf-8",
        )
        tags.append(f'<script src="./{relative}"><\\/script>')
    final = f"chunks/{prefix}-final.js"
    _safe_repo_js(repo_root, final).write_text(
        "(()=>{const b=atob(window."
        + parts_name
        + '.join(""));const u=Uint8Array.from(b,c=>c.charCodeAt(0));'
        + "(0,eval)(new TextDecoder().decode(u));delete window."
        + parts_name
        + "})();\n",
        encoding="utf-8",
    )
    tags.append(f'<script src="./{final}"><\\/script>')
    loader = f"window.{parts_name}=[];document.write('{''.join(tags)}');\n"
    _safe_repo_js(repo_root, script_name).write_text(loader, encoding="utf-8")


def _write_global(repo_root: Path, script_name: str, global_name: str, value: object) -> None:
    source = _assignment_js(global_name, value)
    assert_no_credentials(source)
    if script_name in CHUNKED_GLOBALS and len(source) > DIRECT_JS_LIMIT:
        _write_chunked(repo_root, script_name, source)
        return
    if script_name in CHUNKED_GLOBALS:
        prefix = CHUNKED_GLOBALS[script_name][1]
        chunk_dir = repo_root / "chunks"
        if chunk_dir.is_dir():
            for stale in chunk_dir.glob(f"{prefix}-*.js"):
                stale.unlink()
    _safe_repo_js(repo_root, script_name).write_text(source, encoding="utf-8")


def _bump_cache(index_text: str, token: str) -> str:
    pattern = re.compile(
        r'(src="\./(?:data|duration|extensions|magnetic|refresh|sources|'
        r'drivers-complete|drivers-country-extra|product-origin|delta-[\d-]+)\.js)\?v=[^"]*"'
    )
    updated, count = pattern.subn(lambda match: f'{match.group(1)}?v={token}"', index_text)
    if count < 8:
        raise RefreshError("index.html snapshot script tags could not be cache-busted.")
    return updated


def _cache_token(snapshot: dict) -> str:
    digits = re.sub(r"[^0-9]", "", str(snapshot.get("refreshedAt") or ""))[:12]
    return f"{digits or 'ga4'}-ga4"


def write_live_snapshot(repo_root: Path, bundle: dict, snapshot: dict) -> list[str]:
    """Rewrite the files index.html loads. Returns the relative paths written."""
    index_path = repo_root / "index.html"
    index_text = index_path.read_text(encoding="utf-8")
    scripts = _snapshot_scripts(index_text)
    if "data.js" not in scripts or "sources.js" not in scripts or "drivers-complete.js" not in scripts:
        raise RefreshError("index.html is not loading the live snapshot scripts.")
    written = []
    globals_for = {
        "data.js": ("GA4_DATA", bundle["data"]),
        "duration.js": ("GA4_DURATION", bundle["duration"]),
        "sources.js": ("GA4_SOURCE_DATA", bundle["sources"]),
        "drivers-complete.js": ("GA4_DRIVER_COMPLETE", bundle["drivers"]),
        "extensions.js": ("GA4_BOT_ADJUSTMENTS", bundle["botAdjustments"]),
    }
    for name in scripts:
        if name in globals_for:
            global_name, value = globals_for[name]
            _write_global(repo_root, name, global_name, value)
        elif name == "index.html":
            continue
        else:
            _safe_repo_js(repo_root, name).write_text(NOOP_JS, encoding="utf-8")
        written.append(name)
    token = _cache_token(snapshot)
    index_path.write_text(_bump_cache(index_text, token), encoding="utf-8")
    written.append("index.html")
    for name in written:
        text = (repo_root / name).read_text(encoding="utf-8")
        if "private_key" in text or "BEGIN PRIVATE KEY" in text:
            raise RefreshError(f"Refusing to leave credential material in {name}.")
    return written


def _series_rows(series: dict) -> dict[str, list[tuple]]:
    rows = {}
    for key, points in series.items():
        rows[key] = [
            (point.get("date"), point.get("sessions"), point.get("users"), point.get("views"))
            for point in points
        ]
    return rows


def verify_published(repo_root: Path, bundle: dict) -> None:
    loaded = load_published_snapshot(repo_root)
    expected = bundle["data"]
    actual = loaded["data"]
    if expected["dataStart"] != actual["dataStart"] or expected["dataEnd"] != actual["dataEnd"]:
        raise RefreshError("Published snapshot window does not match the generated files.")
    expected_domains = [(domain_key(domain), domain.get("label")) for domain in expected["domains"]]
    actual_domains = [(domain_key(domain), domain.get("label")) for domain in actual["domains"]]
    if expected_domains != actual_domains:
        raise RefreshError("Published domains do not match the generated snapshot.")
    if _series_rows(expected["series"]) != _series_rows(actual["series"]):
        raise RefreshError("Published series do not match the generated snapshot.")
    for key in bundle["refreshedKeys"]:
        points = actual["series"].get(key) or []
        if not points or not any(point.get("sessions") or point.get("views") for point in points):
            raise RefreshError(f"Generated series for {key} is empty.")
    index_text = (repo_root / "index.html").read_text(encoding="utf-8")
    for name in ("data.js", "duration.js", "sources.js", "drivers-complete.js", "refresh.js"):
        if f"./{name}" not in index_text:
            raise RefreshError(f"index.html no longer references {name}.")


def publish_live(repo_root: Path, snapshot: dict, catalog: dict[str, dict]) -> dict:
    assert_no_credentials(snapshot)
    assert_snapshot_usable(snapshot)
    published = load_published_snapshot(repo_root)
    before = len((published.get("data") or {}).get("domains") or [])
    bundle = merge_live(published, snapshot, catalog)
    after = len(bundle["data"]["domains"])
    if after < before:
        raise RefreshError(
            f"Refusing to publish a snapshot that drops markets ({before} domains to {after})."
        )
    if len(bundle["refreshedKeys"]) * 2 < before and after == len(bundle["refreshedKeys"]):
        raise RefreshError("Refusing to publish a snapshot that drops most markets.")
    write_live_snapshot(repo_root, bundle, snapshot)
    verify_published(repo_root, bundle)
    print(
        f"Published {after} domains; refreshed {len(bundle['refreshedKeys'])} catalog markets "
        f"({bundle['data']['dataStart']} to {bundle['data']['dataEnd']}). "
        f"Content lists keep each month's top {CONTENT_MONTHLY_CAP} paths.",
        flush=True,
    )
    return bundle


def write_snapshot(out_dir: Path, snapshot: dict) -> Path:
    assert_snapshot_usable(snapshot)
    assert_no_credentials(snapshot)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "ga4-snapshot.json"
    if target.name in LIVE_SNAPSHOT_FILES or target.suffix == ".js":
        raise RefreshError("Refusing to overwrite live snapshot JavaScript.")
    if "service" in target.name.lower() and "account" in target.name.lower():
        raise RefreshError("Refusing to write a service-account file.")
    target.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {target} ({target.stat().st_size} bytes).", flush=True)
    return target


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the GA4 snapshot via analytics-mcp.")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="Directory for the new snapshot JSON. Must be snapshot-refresh/ inside this repo.",
    )
    parser.add_argument("--start", default=os.environ.get("GA4_START_DATE", ""), help="Inclusive YYYY-MM-DD.")
    parser.add_argument("--end", default=os.environ.get("GA4_END_DATE", ""), help="Inclusive YYYY-MM-DD.")
    parser.add_argument(
        "--property-ids",
        default=os.environ.get("GA4_PROPERTY_IDS", ""),
        help="Comma-separated GA4 property IDs. Default: every property the service account can read.",
    )
    parser.add_argument(
        "--from-json",
        default="",
        help="Reuse an existing snapshot JSON instead of calling GA4. The file is not copied into the repo.",
    )
    parser.add_argument(
        "--publish-live",
        action="store_true",
        help="Rewrite the live dashboard JavaScript from this snapshot.",
    )
    return parser.parse_args(argv)


def load_snapshot_json(path: Path) -> dict:
    if not path.is_file():
        raise RefreshError(f"Snapshot JSON not found: {path.name}.")
    text = path.read_text(encoding="utf-8")
    assert_no_credentials(text)
    try:
        snapshot = json.loads(text)
    except json.JSONDecodeError:
        raise RefreshError("Snapshot JSON is not valid JSON.") from None
    if not isinstance(snapshot, dict):
        raise RefreshError("Snapshot JSON must be an object.")
    assert_snapshot_usable(snapshot)
    return snapshot


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    catalog = load_catalog()
    if args.from_json:
        snapshot = load_snapshot_json(Path(args.from_json))
    else:
        out_dir = assert_safe_output(Path(args.out))
        start, end = resolve_dates(args.start, args.end)
        only = {part.strip() for part in args.property_ids.split(",") if part.strip()} or None
        info = parse_service_account(os.environ.get(SECRET_ENV))
        with ServiceAccountCredentials(info):
            snapshot = asyncio.run(refresh(info, start, end, only, catalog))
        write_snapshot(out_dir, snapshot)
    if args.publish_live:
        publish_live(REPO_ROOT, snapshot, catalog)


if __name__ == "__main__":
    main()

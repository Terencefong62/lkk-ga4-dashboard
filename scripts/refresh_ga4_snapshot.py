#!/usr/bin/env python3
"""Refresh a GA4 dashboard snapshot with Google's official Analytics MCP server.

The official server is the PyPI package and console script ``analytics-mcp``
(entrypoint also published as ``google-analytics-mcp``) from
https://github.com/googleanalytics/google-analytics-mcp. This job calls that
server's read-only tools (``get_account_summaries`` and ``run_report``).

Credentials are read only from the ``GA4_SERVICE_ACCOUNT_JSON`` environment
variable, which must be the service-account JSON itself. The script never
reads a key file from the repo and never writes the key into the snapshot.

Refreshed data is written under ``snapshot-refresh/`` as JSON. The published
dashboard JavaScript (``data.js``, ``chunks/``, ``refresh.js``, and the other
live snapshot files) is left unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import stat
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

# Published files the dashboard loads. A refresh must not replace them.
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

DAILY_METRICS = [
    "sessions",
    "activeUsers",
    "screenPageViews",
    "averageSessionDuration",
]
PAGE_SIZE = 100_000


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
        "refreshedAt": refreshed_at,
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


async def fetch_rows(run_report, property_id: str, *, dimensions, metrics, start: str, end: str, dimension_filter=None) -> list[dict]:
    rows = []
    offset = 0
    while True:
        report = await run_report(
            property_id=property_id,
            date_ranges=[{"start_date": start, "end_date": end, "name": "snapshot"}],
            dimensions=dimensions,
            metrics=metrics,
            dimension_filter=dimension_filter,
            limit=PAGE_SIZE,
            offset=offset or None,
            return_property_quota=False,
        )
        if not isinstance(report, dict):
            raise RefreshError(f"run_report for property {property_id} returned an unexpected response.")
        batch = report.get("rows") or []
        rows.extend(batch)
        row_count = int(report.get("row_count") or 0)
        offset += len(batch)
        if not batch or offset >= row_count:
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
            rows = await fetch_rows(
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
    refreshed_at = dt.datetime.now(HKT).isoformat(timespec="seconds")
    return finalize_snapshot(
        collected,
        start=start,
        end=end,
        properties=properties,
        catalog=catalog,
        refreshed_at=refreshed_at,
    )


def write_snapshot(out_dir: Path, snapshot: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "ga4-snapshot.json"
    if target.name in LIVE_SNAPSHOT_FILES:
        raise RefreshError("Refusing to overwrite live snapshot JavaScript.")
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    out_dir = assert_safe_output(Path(args.out))
    start, end = resolve_dates(args.start, args.end)
    only = {part.strip() for part in args.property_ids.split(",") if part.strip()} or None
    info = parse_service_account(os.environ.get(SECRET_ENV))
    catalog = load_catalog()
    with ServiceAccountCredentials(info):
        snapshot = asyncio.run(refresh(info, start, end, only, catalog))
    write_snapshot(out_dir, snapshot)


if __name__ == "__main__":
    main()

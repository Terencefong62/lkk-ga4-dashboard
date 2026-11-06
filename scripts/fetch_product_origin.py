#!/usr/bin/env python3
"""Build product-origin.js: previous page for top product and recipe pages.

Product pages class the previous page (GA4 pageReferrer) as:
  recipe detail   /recipe/{slug} or /recipes/{slug}
  product listing /products, or /products/saucetype/...
  other           everything else, including another product detail page

Recipe pages class the previous page as:
  product detail  /products/{slug}, not the products index or a sauce-type page
  recipe listing  a path that ends at /recipe or /recipes
  other           everything else, including another recipe detail page

Daily counts follow the dashboard's day index (dataStart = day 0) so the
selected period can sum them. The service-account JSON is never written.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from refresh_ga4_snapshot import (  # noqa: E402
    ServiceAccountCredentials,
    fetch_report_rows,
    iso_from_ga4,
    parse_service_account,
)

# Property that actually owns each consumer host. Leakage from another
# property (a handful of page views recorded under the USA property, for
# example) is ignored.
HOST_PROPERTY = {
    "usa.lkk.com": "398685110",
    "hk.lkk.com": "377253102",
    "ca.lkk.com": "370921236",
    "au.lkk.com": "370583564",
    "nz.lkk.com": "370583564",
    "malaysia.lkk.com": "367098012",
    "sg.lkk.com": "367881323",
    "tw.lkk.com": "372928120",
    "jp.lkk.com": "359271345",
    "kr.lkk.com": "365539943",
    "in.lkk.com": "364795029",
    "vn.lkk.com": "368579958",
    "id.lkk.com": "365064368",
    "ph.lkk.com": "367186392",
    "csa.lkk.com": "365051596",
    "eu.lkk.com": "365568280",
}

RECIPE_DETAIL = re.compile(r"/recipes?/[^/?#]+", re.IGNORECASE)
PRODUCT_LISTING = re.compile(r"/products(?:/saucetype(?:/[^/?#]*)?)?$", re.IGNORECASE)
PRODUCT_DETAIL = re.compile(
    r"/products/(?!saucetype(?:/|$)|attributestags(?:/|$))[^/?#]+$",
    re.IGNORECASE,
)
RECIPE_LISTING = re.compile(r"/recipes?$", re.IGNORECASE)


def _referrer_path(url: str) -> str:
    text = (url or "").strip()
    if not text or text == "(not set)":
        return ""
    path = urlparse(text).path if "://" in text else text
    return path.split("?", 1)[0].rstrip("/")


def referrer_class(url: str) -> str:
    """Return recipe, listing, or other for a product page's previous page."""
    path = _referrer_path(url)
    if not path:
        return "other"
    if RECIPE_DETAIL.search(path):
        return "recipe"
    if PRODUCT_LISTING.search(path):
        return "listing"
    return "other"


def recipe_referrer_class(url: str) -> str:
    """Return product, listing, or other for a recipe page's previous page."""
    path = _referrer_path(url)
    if not path:
        return "other"
    if PRODUCT_DETAIL.search(path):
        return "product"
    if RECIPE_LISTING.search(path):
        return "listing"
    return "other"


def _load_content_paths(kind: str, needle: str) -> tuple[str, str, dict[str, list[str]]]:
    encoded = []
    chunk_dir = REPO / "chunks"
    files = sorted(
        (p for p in chunk_dir.glob("data-*.js") if re.fullmatch(r"data-\d+\.js", p.name)),
        key=lambda p: int(p.name.split("-")[1].split(".")[0]),
    )
    for path in files:
        match = re.search(r'push\("([^"]*)"\)', path.read_text(encoding="utf-8"))
        if not match:
            raise SystemExit(f"Could not read data chunk {path.name}")
        encoded.append(match.group(1))
    source = base64.b64decode("".join(encoded)).decode("utf-8")
    marker = "window.GA4_DATA="
    payload = json.loads(source[source.index(marker) + len(marker) :].rstrip().rstrip(";"))
    paths: dict[str, list[str]] = {}
    for key, kinds in (payload.get("content") or {}).items():
        section = (kinds or {}).get(kind) or {}
        values = []
        for pair in section.get("paths") or []:
            if not pair:
                continue
            page = str(pair[0])
            if needle in page.lower():
                values.append(page)
        if values and key in HOST_PROPERTY:
            paths[key] = sorted(set(values))
    return payload["dataStart"], payload["dataEnd"], paths


def _contains(field: str, value: str) -> dict:
    return {
        "filter": {
            "field_name": field,
            "string_filter": {"match_type": "CONTAINS", "value": value, "case_sensitive": False},
        }
    }


def _exact(field: str, value: str) -> dict:
    return {
        "filter": {
            "field_name": field,
            "string_filter": {"match_type": "EXACT", "value": value, "case_sensitive": False},
        }
    }


def _in_list(field: str, values: list[str]) -> dict:
    return {"filter": {"field_name": field, "in_list_filter": {"values": values, "case_sensitive": False}}}


def _and(*parts: dict) -> dict:
    return {"and_group": {"expressions": list(parts)}}


def _day_index(date: str, start: str) -> int:
    from datetime import date as date_cls

    return (date_cls.fromisoformat(date) - date_cls.fromisoformat(start)).days


async def _rows_for_host(run_report, property_id: str, host: str, paths: list[str], start: str, end: str, needle: str):
    batches = [paths[index : index + 40] for index in range(0, len(paths), 40)]
    rows = []
    for batch in batches:
        try:
            found = await fetch_report_rows(
                run_report,
                property_id,
                dimensions=["date", "pagePath", "pageReferrer"],
                metrics=["screenPageViews"],
                start=start,
                end=end,
                dimension_filter=_and(_exact("hostName", host), _in_list("pagePath", batch)),
            )
        except Exception as exc:
            print(f"  in-list failed for {host} ({type(exc).__name__}); reading paths containing {needle}", flush=True)
            found = await fetch_report_rows(
                run_report,
                property_id,
                dimensions=["date", "pagePath", "pageReferrer"],
                metrics=["screenPageViews"],
                start=start,
                end=end,
                dimension_filter=_and(_exact("hostName", host), _contains("pagePath", needle)),
            )
            wanted = {path.lower() for path in paths}
            found = [row for row in found if row["dimension_values"][1]["value"].lower() in wanted]
        rows.extend(found)
    return rows


async def _build_kind(run_report, kind: str, needle: str, classifier, slots: dict[str, int]) -> dict:
    start, end, content_paths = _load_content_paths(kind, needle)
    origin: dict[str, dict[str, list[list[int]]]] = {}
    product_recipes: dict[str, dict] = {}
    for host, paths in sorted(content_paths.items()):
        property_id = HOST_PROPERTY[host]
        print(f"{kind} {host} via {property_id}: {len(paths)} paths", flush=True)
        rows = await _rows_for_host(run_report, property_id, host, paths, start, end, needle)
        buckets: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
        recipe_sources: dict[str, dict[str, dict[int, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        known = {path.lower(): path for path in paths}
        for row in rows:
            date = row["dimension_values"][0]["value"]
            page = row["dimension_values"][1]["value"]
            referrer = row["dimension_values"][2]["value"]
            views = int(float(row["metric_values"][0]["value"] or 0))
            if views <= 0:
                continue
            canonical = known.get(page.lower())
            if not canonical:
                continue
            day = _day_index(iso_from_ga4(date), start)
            slot_name = classifier(referrer)
            buckets[canonical][day][slots[slot_name]] += views
            if kind == "product" and slot_name == "recipe":
                recipe_path = _referrer_path(referrer)
                if recipe_path:
                    recipe_sources[canonical][recipe_path][day] += views
        host_out = {}
        for path, days in buckets.items():
            records = [
                [day, counts[0], counts[1], counts[2]]
                for day, counts in sorted(days.items())
                if any(counts)
            ]
            if records:
                host_out[path] = records
        if host_out:
            origin[host] = host_out
        if kind == "product" and recipe_sources:
            compact = {}
            for product, recipes in recipe_sources.items():
                recipe_out = {
                    recipe: [[day, views] for day, views in sorted(days.items()) if views]
                    for recipe, days in recipes.items()
                }
                recipe_out = {recipe: rows for recipe, rows in recipe_out.items() if rows}
                if recipe_out:
                    compact[product] = recipe_out
            if compact:
                product_recipes[host] = compact
        print(f"  {len(rows)} source rows -> {len(host_out)} paths", flush=True)
    return origin, product_recipes


async def build() -> tuple[dict, dict, dict]:
    from analytics_mcp.tools.reporting.core import run_report

    info = parse_service_account(os.environ.get("GA4_SERVICE_ACCOUNT_JSON"))
    product_slots = {"recipe": 0, "listing": 1, "other": 2}
    recipe_slots = {"product": 0, "listing": 1, "other": 2}
    with ServiceAccountCredentials(info):
        product, from_recipes = await _build_kind(run_report, "product", "/product", referrer_class, product_slots)
        recipe, _ignored = await _build_kind(run_report, "recipe", "/recipe", recipe_referrer_class, recipe_slots)
    return product, recipe, from_recipes


def write_origin(product: dict, recipe: dict, from_recipes: dict) -> Path:
    target = REPO / "product-origin.js"
    product_payload = json.dumps(product, ensure_ascii=False, separators=(",", ":"))
    recipe_payload = json.dumps(recipe, ensure_ascii=False, separators=(",", ":"))
    from_payload = json.dumps(from_recipes, ensure_ascii=False, separators=(",", ":"))
    source = (
        f"window.GA4_PRODUCT_ORIGIN={product_payload};\n"
        f"window.GA4_RECIPE_ORIGIN={recipe_payload};\n"
        f"window.GA4_PRODUCT_FROM_RECIPES={from_payload};\n"
    )
    if "private_key" in source or "service_account" in source:
        raise SystemExit("Refusing to write credentials into product-origin.js")
    target.write_text(source, encoding="utf-8")
    print(f"Wrote {target.name} ({target.stat().st_size} bytes)", flush=True)
    return target


def main() -> None:
    product, recipe, from_recipes = asyncio.run(build())
    write_origin(product, recipe, from_recipes)


if __name__ == "__main__":
    main()

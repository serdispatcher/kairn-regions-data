#!/usr/bin/env python3
"""Freshness checker, per-country pack builder, and manifest merger for kairn-regions-data."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, SCRIPT_DIR)

from osm_pack_builder import (  # noqa: E402
    HgtTileSampler,
    build_region_pack,
    extract_and_merge_pbfs,
    fetch_geofabrik_timestamp,
    parse_iso_utc,
)
from regions_catalog import COUNTRIES, REGIONS  # noqa: E402


def cmd_check_only(args):
    manifest = json.load(open(args.manifest, encoding="utf-8"))
    reg_map = manifest.get("regions", {})
    target_countries = COUNTRIES if args.country == "all" else [args.country]
    for c in target_countries:
        if c not in COUNTRIES:
            raise ValueError(f"Unknown country: {c}")

    # Collect unique Geofabrik URLs for selected countries
    selected_regs = [r for r in REGIONS if r["country"] in target_countries]
    unique_urls = sorted({u for r in selected_regs for u in r["pbf_urls"]})
    print(f"Checking Geofabrik freshness across {len(unique_urls)} PBF sources...", flush=True)
    remote_ts_cache = {}
    for url in unique_urls:
        remote_ts_cache[url] = fetch_geofabrik_timestamp(url)

    min_age = timedelta(days=max(0.0, args.min_age_days))
    now_utc = datetime.now(timezone.utc)
    countries_to_build = []
    summary_rows = []

    for c in target_countries:
        c_regs = [r for r in selected_regs if r["country"] == c]
        c_urls = sorted({u for r in c_regs for u in r["pbf_urls"]})
        remote_dts = [parse_iso_utc(remote_ts_cache.get(u)) for u in c_urls]
        remote_dts = [d for d in remote_dts if d is not None]
        remote_max_dt = max(remote_dts) if remote_dts else None
        remote_str = remote_max_dt.strftime("%Y-%m-%d %H:%M UTC") if remote_max_dt else "unknown"

        saved_dts = []
        missing_ts = False
        for r in c_regs:
            m_entry = reg_map.get(r["id"], {})
            dt = parse_iso_utc(m_entry.get("osm_timestamp"))
            if dt is None:
                missing_ts = True
            else:
                saved_dts.append(dt)
        saved_min_dt = min(saved_dts) if (saved_dts and not missing_ts) else None
        saved_str = saved_min_dt.strftime("%Y-%m-%d %H:%M UTC") if saved_min_dt else "none"

        needs_update = False
        reason = "up-to-date"
        if args.force:
            needs_update = True
            reason = "forced"
        elif missing_ts or saved_min_dt is None:
            needs_update = True
            reason = "missing osm_timestamp"
        elif remote_max_dt is not None:
            age = remote_max_dt - saved_min_dt
            if age >= min_age and (min_age > timedelta(0) or age > timedelta(0)):
                needs_update = True
                reason = f"stale ({age.total_seconds() / 86400:.1f}d >= {args.min_age_days:g}d)"
            else:
                reason = f"fresh ({age.total_seconds() / 86400:.1f}d < {args.min_age_days:g}d)"
        else:
            age = now_utc - saved_min_dt
            if age >= min_age and min_age > timedelta(0):
                needs_update = True
                reason = f"remote unreachable, local age {age.total_seconds() / 86400:.1f}d"
            else:
                reason = "remote unreachable, within local threshold"

        if needs_update:
            countries_to_build.append(c)
        status_icon = "UPDATE" if needs_update else "SKIP"
        print(f"  [{status_icon:6s}] {c:14s} saved={saved_str:20s} geofabrik={remote_str:20s} -> {reason}", flush=True)
        summary_rows.append((c, saved_str, remote_str, status_icon, reason))

    has_updates = "true" if countries_to_build else "false"
    matrix_json = json.dumps(countries_to_build)
    print(f"\nhas_updates={has_updates} countries_matrix={matrix_json}", flush=True)

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"has_updates={has_updates}\n")
            f.write(f"countries_matrix={matrix_json}\n")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("### Kairn Regions Freshness Check\n\n")
            f.write("| Country | Saved OSM | Geofabrik Latest | Action | Reason |\n")
            f.write("| --- | --- | --- | --- | --- |\n")
            for c, s_str, r_str, icon, reason in summary_rows:
                f.write(f"| `{c}` | `{s_str}` | `{r_str}` | **{icon}** | {reason} |\n")
            f.write("\n")


def cmd_build_country(args):
    country = args.country
    if country not in COUNTRIES:
        raise ValueError(f"Unknown country: {country}")
    t0 = time.time()
    manifest = json.load(open(args.manifest, encoding="utf-8"))
    reg_map = manifest.get("regions", {})
    c_regs = [r for r in REGIONS if r["country"] == country]

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(args.work_dir, exist_ok=True)
    geonames_dir = os.path.join(args.work_dir, "geonames")
    tiles_dir = os.path.join(os.path.dirname(os.path.abspath(args.manifest)), "tiles")

    sampler = HgtTileSampler(
        tiles_dir=tiles_dir if os.path.isdir(tiles_dir) else None,
        repo_raw_base_url=f"https://raw.githubusercontent.com/{args.repo}/main",
        cache_dir=os.path.join(args.work_dir, "hgt_cache"),
    )

    # Fetch remote OSM timestamps for PBFs used by this country
    pbf_urls = sorted({u for r in c_regs for u in r["pbf_urls"]})
    ts_by_url = {u: fetch_geofabrik_timestamp(u) for u in pbf_urls}

    pbf_paths = extract_and_merge_pbfs(c_regs, args.work_dir)
    release_base_url = f"https://github.com/{args.repo}/releases/download/{args.release_tag}"

    built = {}
    for reg in c_regs:
        rid = reg["id"]
        dts = [parse_iso_utc(ts_by_url.get(u)) for u in reg["pbf_urls"]]
        dts = [d for d in dts if d is not None]
        osm_ts = max(dts).strftime("%Y-%m-%dT%H:%M:%SZ") if dts else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        built[rid] = build_region_pack(
            reg=reg,
            pbf_path=pbf_paths[rid],
            sampler=sampler,
            geonames_dir=geonames_dir,
            out_assets_dir=args.out_dir,
            prev_entry=reg_map.get(rid),
            osm_timestamp=osm_ts,
            release_base_url=release_base_url,
            work_dir=args.work_dir,
        )

    os.makedirs(os.path.dirname(os.path.abspath(args.meta_out)), exist_ok=True)
    with open(args.meta_out, "w", encoding="utf-8") as f:
        json.dump({"country": country, "regions": built}, f, ensure_ascii=False, indent=2)

    shutil.rmtree(args.work_dir, ignore_errors=True)
    print(f"Finished country '{country}' ({len(built)} regions) in {time.time() - t0:.1f}s.", flush=True)


def cmd_merge_meta(args):
    manifest = json.load(open(args.manifest, encoding="utf-8"))
    reg_map = manifest.setdefault("regions", {})
    merged_count = 0

    for root, _, files in os.walk(args.meta_dir):
        for fname in sorted(files):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(root, fname)
            payload = json.load(open(fpath, encoding="utf-8"))
            for rid, entry in payload.get("regions", {}).items():
                reg_map[rid] = entry
                merged_count += 1

    if merged_count > 0:
        manifest["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(args.manifest, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f"Merged {merged_count} region entries into {args.manifest}.", flush=True)
    else:
        print("No metadata files found to merge; leaving manifest untouched.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Kairn regional offline packs updater")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-only", action="store_true", help="Check Geofabrik freshness against manifest.json")
    mode.add_argument("--build-country", metavar="COUNTRY", help="Rebuild all regions for a single country")
    mode.add_argument("--merge-meta", metavar="META_DIR", help="Merge per-country meta JSON files into manifest.json")

    parser.add_argument("--manifest", default=os.path.join(REPO_ROOT, "manifest.json"))
    parser.add_argument("--country", default="all", help="Country filter for --check-only ('all' or country id)")
    parser.add_argument("--min-age-days", type=float, default=21.0, help="Minimum OSM age delta in days before rebuild")
    parser.add_argument("--force", action="store_true", help="Rebuild selected country/countries even if fresh")
    parser.add_argument("--github-output", default="", help="Path to $GITHUB_OUTPUT file")
    parser.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "build", "assets"))
    parser.add_argument("--meta-out", default=os.path.join(REPO_ROOT, "build", "meta.json"))
    parser.add_argument("--work-dir", default=os.path.join(REPO_ROOT, "build", "work"))
    parser.add_argument("--release-tag", default="regions-latest")
    parser.add_argument("--repo", default="serdispatcher/kairn-regions-data")

    args = parser.parse_args()
    if args.check_only:
        cmd_check_only(args)
    elif args.build_country:
        args.country = args.build_country
        cmd_build_country(args)
    elif args.merge_meta:
        args.meta_dir = args.merge_meta
        cmd_merge_meta(args)


if __name__ == "__main__":
    main()

# Kairn Regional Offline Map Data

Pre-built offline regional topographic packs for **Kairn** (`https://github.com/serdispatcher/kairn`):
- `manifest.json` — regional catalog, versions, `osm_timestamp`, download URLs, sizes, and SHA-256 hashes.
- **GitHub Releases (`regions-latest`)** — automated monthly vector/POI pack updates (`<region_id>__trails.bin.gz`, `<region_id>__pois.json.gz`, `<region_id>__water.bin.gz`) published via GitHub Actions so `.git` history stays compact.
- `regions/<region_id>/...` — initial baseline vector/POI packs preserved for backward compatibility.
- `tiles/<NxxEyyy>.hgt.gz` — `1°×1°` 3″ SRTM elevation tiles (`1201×1201` signed int16 BE).

## Automated Refresh Workflow
- Workflow: `.github/workflows/update-regions.yml` runs on the **1st of each month at 03:17 UTC** (`cron: '17 3 1 * *'`) and on manual `workflow_dispatch`.
- Compares Geofabrik extract timestamps (`state.txt`) against per-region `osm_timestamp` in `manifest.json`.
- Rebuilds stale countries in parallel using `osmium-tool`, uploads updated packs to release `regions-latest`, and commits only `manifest.json`.

## Attribution & License
- Vector & POI data: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) (ODbL).
- Elevation data: SRTM / NASA / USGS via AWS Terrain Tiles.

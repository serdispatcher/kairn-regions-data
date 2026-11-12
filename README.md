# Kairn Regional Offline Map Data

Pre-built offline regional topographic packs for **Kairn** (`https://github.com/serdispatcher/kairn`):
- `manifest.json` — regional catalog, versions, sizes, and SHA-256 hashes.
- `regions/<region_id>/trails.bin` (`.gz`) — compact `KTR1` binary vector pack (hiking trails, roads, tracks, rivers, streams, lakes, coastline, piers, forests, national parks).
- `regions/<region_id>/pois.json` (`.gz`) — peaks, passes, huts/camps, springs/waterfalls, caves, viewpoints, ancient ruins, and settlements with DEM-sampled elevations.
- `tiles/<NxxEyyy>.hgt.gz` — `1°×1°` 3″ SRTM elevation tiles (`1201×1201` signed int16 BE).

## Attribution & License
- Vector & POI data: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) (ODbL).
- Elevation data: SRTM / NASA / USGS via AWS Terrain Tiles.

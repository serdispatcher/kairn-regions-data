"""Self-contained OSM + GeoNames + SRTM offline vector pack builder for kairn-regions-data.

Uses only Python 3 standard library, `osmium` CLI, and `curl`.
"""
import array
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile

from regions_catalog import CURATED_PEAKS

USER_AGENT = "KairnRegionsSync/1.0"


def fetch_geofabrik_timestamp(pbf_url):
    """Returns ISO-8601 UTC timestamp string (YYYY-MM-DDTHH:MM:SSZ) for a Geofabrik extract, or None."""
    state_url = pbf_url.replace("-latest.osm.pbf", "-updates/state.txt")
    for attempt in range(3):
        try:
            req = urllib.request.Request(state_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", "ignore")
            for line in body.splitlines():
                if line.startswith("timestamp="):
                    raw_ts = line.split("=", 1)[1].strip().replace("\\:", ":").rstrip("\r")
                    dt = parse_iso_utc(raw_ts)
                    if dt is not None:
                        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            if attempt < 2:
                time.sleep(2)

    # Fallback: HTTP HEAD Last-Modified on the PBF URL
    for attempt in range(3):
        try:
            req = urllib.request.Request(pbf_url, method="HEAD", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                lm = resp.headers.get("Last-Modified")
                if lm:
                    dt = parsedate_to_datetime(lm).astimezone(timezone.utc)
                    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            if attempt < 2:
                time.sleep(2)
    return None


def parse_iso_utc(ts_str):
    if not ts_str:
        return None
    s = ts_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def download_file(url, dst):
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    print(f"Downloading {url} -> {dst}...", flush=True)
    for attempt in range(20):
        res = subprocess.run(
            [
                "curl",
                "-fL",
                "-C",
                "-",
                "--connect-timeout",
                "20",
                "--speed-time",
                "20",
                "--speed-limit",
                "10240",
                "-A",
                USER_AGENT,
                "-o",
                tmp,
                url,
            ]
        )
        if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dst)
            print(f"Downloaded {os.path.basename(dst)} ({os.path.getsize(dst) / 1024 / 1024:.1f} MB)", flush=True)
            return
        print(f"  curl exited {res.returncode}, retrying ({attempt + 2}/20)...", flush=True)
        time.sleep(2)
    raise RuntimeError(f"Failed to download {url} after 20 attempts")


class HgtTileSampler:
    """Samples 1201x1201 SRTM .hgt.gz elevation tiles lazily (local tiles_dir or remote raw GitHub)."""

    def __init__(
        self,
        tiles_dir=None,
        repo_raw_base_url="https://raw.githubusercontent.com/serdispatcher/kairn-regions-data/main",
        cache_dir="/tmp/kairn_hgt_cache",
    ):
        self.tiles_dir = tiles_dir
        self.repo_raw_base_url = repo_raw_base_url.rstrip("/")
        self.cache_dir = cache_dir
        self._mem = {}
        self._missing = set()

    def _tile_array(self, la, lo):
        ns = "N" if la >= 0 else "S"
        ew = "E" if lo >= 0 else "W"
        fname = f"{ns}{abs(la):02d}{ew}{abs(lo):03d}.hgt.gz"
        if fname in self._mem:
            return self._mem[fname]
        if fname in self._missing:
            return None

        gz_bytes = None
        if self.tiles_dir:
            local_path = os.path.join(self.tiles_dir, fname)
            if os.path.exists(local_path):
                with open(local_path, "rb") as f:
                    gz_bytes = f.read()

        if gz_bytes is None:
            cached_path = os.path.join(self.cache_dir, fname)
            if os.path.exists(cached_path) and os.path.getsize(cached_path) > 0:
                with open(cached_path, "rb") as f:
                    gz_bytes = f.read()
            else:
                url = f"{self.repo_raw_base_url}/tiles/{fname}"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        gz_bytes = resp.read()
                    os.makedirs(self.cache_dir, exist_ok=True)
                    tmp = cached_path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(gz_bytes)
                    os.replace(tmp, cached_path)
                except Exception:
                    self._missing.add(fname)
                    return None

        try:
            raw = gzip.decompress(gz_bytes)
            if len(raw) != 1201 * 1201 * 2:
                self._missing.add(fname)
                return None
            arr = array.array("h")
            arr.frombytes(raw)
            if sys.byteorder == "little":
                arr.byteswap()
            # Keep memory bounded on runners
            if len(self._mem) >= 24:
                self._mem.pop(next(iter(self._mem)))
            self._mem[fname] = arr
            return arr
        except Exception:
            self._missing.add(fname)
            return None

    def sample_elev(self, lat, lon):
        la = int(math.floor(lat))
        lo = int(math.floor(lon))
        arr = self._tile_array(la, lo)
        if arr is None:
            return 0.0
        row = max(0, min(1200, int(round((1.0 - (lat - la)) * 1200.0))))
        col = max(0, min(1200, int(round((lon - lo) * 1200.0))))
        val = arr[row * 1201 + col]
        if val <= -32768 or val < -500 or val > 9000:
            return 0.0
        return float(val)


def rdp_simplify(pts, eps_deg):
    if len(pts) <= 2:
        return pts
    keep = [False] * len(pts)
    keep[0] = True
    keep[-1] = True
    stack = [(0, len(pts) - 1)]
    eps_sq = eps_deg * eps_deg
    while stack:
        i, j = stack.pop()
        if j - i <= 1:
            continue
        ax, ay = pts[i][1], pts[i][0]
        bx, by = pts[j][1], pts[j][0]
        dx, dy = bx - ax, by - ay
        len_sq = dx * dx + dy * dy
        max_d2 = -1.0
        max_k = -1
        for k in range(i + 1, j):
            px, py = pts[k][1], pts[k][0]
            if len_sq <= 1e-16:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
                proj_x = ax + t * dx
                proj_y = ay + t * dy
                d2 = (px - proj_x) ** 2 + (py - proj_y) ** 2
            if d2 > max_d2:
                max_d2 = d2
                max_k = k
        if max_d2 > eps_sq and max_k != -1:
            keep[max_k] = True
            stack.append((i, max_k))
            stack.append((max_k, j))
    return [pts[idx] for idx in range(len(pts)) if keep[idx]]


def encode_ktr1(trails):
    buf = bytearray(b"KTR1")
    valid_records = []

    def split_large_jumps(pts):
        out_pts = [pts[0]]
        for k in range(1, len(pts)):
            la0, lo0 = out_pts[-1]
            la1, lo1 = pts[k]
            steps = max(1, int(math.ceil(max(abs(la1 - la0), abs(lo1 - lo0)) / 0.25)))
            for s in range(1, steps + 1):
                f = s / steps
                out_pts.append((la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f))
        return out_pts

    for t in trails:
        if len(t["points"]) < 2:
            continue
        pts = split_large_jumps(t["points"])
        if len(pts) < 2:
            continue
        if t.get("kind") == 15 and t.get("id", "").startswith("tr_ne_land_"):
            valid_records.append((t, t["id"], pts))
        else:
            for chunk_start in range(0, len(pts) - 1, 3500):
                sub = pts[chunk_start:chunk_start + 3501]
                if len(sub) < 2:
                    continue
                rec_id = t["id"] if chunk_start == 0 else f"{t['id']}_{chunk_start}"
                valid_records.append((t, rec_id, sub))

    buf.extend(struct.pack(">I", len(valid_records)))
    for t, rec_id, pts in valid_records:
        flags = (1 if t["is_marked"] else 0) | ((t["difficulty"] & 3) << 1) | ((t["kind"] & 31) << 3)
        buf.append(flags)
        for s in (rec_id, t.get("name") or "", t.get("ref") or ""):
            b = s.encode("utf-8")[:240]
            buf.append(len(b))
            buf.extend(b)
        buf.extend(struct.pack(">H", len(pts)))
        lat0 = int(round(pts[0][0] * 1e5))
        lon0 = int(round(pts[0][1] * 1e5))
        buf.extend(struct.pack(">ii", lat0, lon0))
        prev_lat, prev_lon = lat0, lon0
        for p in pts[1:]:
            cur_lat = int(round(p[0] * 1e5))
            cur_lon = int(round(p[1] * 1e5))
            dlat = max(-32767, min(32767, cur_lat - prev_lat))
            dlon = max(-32767, min(32767, cur_lon - prev_lon))
            buf.extend(struct.pack(">hh", dlat, dlon))
            prev_lat += dlat
            prev_lon += dlon
    return bytes(buf)


def iter_geojsonseq(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.lstrip("\x1e").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def extract_rings(geom):
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon" and coords:
        return [coords[0]]
    if gtype == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    if gtype == "LineString":
        return [coords]
    if gtype == "MultiLineString":
        return list(coords)
    return []


def _slug_from_url(url):
    return url.split("/")[-1].replace("-latest.osm.pbf", "")


def _merge_and_dedupe_pbfs(part_paths, dst_pbf, work_dir):
    merged_pbf = os.path.join(work_dir, os.path.basename(dst_pbf) + ".merged.osm.pbf")
    deduped_opl = os.path.join(work_dir, os.path.basename(dst_pbf) + ".dedup.opl")
    subprocess.run(["osmium", "merge", "-O", *part_paths, "-o", merged_pbf], check=True)
    proc = subprocess.Popen(
        ["osmium", "cat", "-f", "opl", merged_pbf],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    seen = set()
    with open(deduped_opl, "w", encoding="utf-8") as out_f:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line:
                continue
            sp = line.find(" ")
            head = line[:sp] if sp > 0 else line.strip()
            if head and head[0] in ("n", "w", "r") and head[1:].isdigit():
                key = (head[0], head[1:])
                if key in seen:
                    continue
                seen.add(key)
            out_f.write(line)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"osmium cat exited {rc}")
    subprocess.run(["osmium", "cat", "-O", "-F", "opl", deduped_opl, "-o", dst_pbf], check=True)
    for p in (merged_pbf, deduped_opl):
        if os.path.exists(p):
            os.remove(p)


def extract_and_merge_pbfs(regions, work_dir):
    """Downloads source Geofabrik PBFs one by one, extracts region bboxes, and deletes source PBFs immediately."""
    os.makedirs(work_dir, exist_ok=True)
    url_order = []
    for reg in regions:
        for u in reg["pbf_urls"]:
            if u not in url_order:
                url_order.append(u)

    for url in url_order:
        slug = _slug_from_url(url)
        regs = [r for r in regions if url in r["pbf_urls"]]
        pending = []
        for reg in regs:
            rid = reg["id"]
            final_pbf = os.path.join(work_dir, f"{rid}.osm.pbf")
            part_pbf = os.path.join(work_dir, f"{rid}__{slug}.osm.pbf")
            if not os.path.exists(final_pbf) and not os.path.exists(part_pbf):
                pending.append((reg, part_pbf))
        if not pending:
            continue

        src_pbf = os.path.join(work_dir, f"{slug}-latest.osm.pbf")
        download_file(url, src_pbf)
        for reg, part_pbf in pending:
            min_la = min(t[0] for t in reg["tiles"]) - 0.15
            max_la = max(t[0] for t in reg["tiles"]) + 1.15
            min_lo = min(t[1] for t in reg["tiles"]) - 0.15
            max_lo = max(t[1] for t in reg["tiles"]) + 1.15
            bbox = f"{min_lo},{min_la},{max_lo},{max_la}"
            print(f"  Extracting {reg['id']} from {slug} (bbox={bbox})...", flush=True)
            subprocess.run(["osmium", "extract", "-O", "--bbox", bbox, src_pbf, "-o", part_pbf], check=True)
        if os.path.exists(src_pbf):
            os.remove(src_pbf)
            print(f"  Removed {os.path.basename(src_pbf)} to free runner disk space.", flush=True)

    out_paths = {}
    for reg in regions:
        rid = reg["id"]
        final_pbf = os.path.join(work_dir, f"{rid}.osm.pbf")
        if not os.path.exists(final_pbf):
            parts = [os.path.join(work_dir, f"{rid}__{_slug_from_url(u)}.osm.pbf") for u in reg["pbf_urls"]]
            if len(parts) == 1:
                os.replace(parts[0], final_pbf)
            else:
                _merge_and_dedupe_pbfs(parts, final_pbf, work_dir)
                for p in parts:
                    if os.path.exists(p):
                        os.remove(p)
        out_paths[rid] = final_pbf
    return out_paths


def ensure_geonames(code, geonames_dir):
    txt_path = os.path.join(geonames_dir, f"{code}.txt")
    if os.path.exists(txt_path) and os.path.getsize(txt_path) > 0:
        return txt_path
    os.makedirs(geonames_dir, exist_ok=True)
    zip_path = os.path.join(geonames_dir, f"{code}.zip")
    download_file(f"https://download.geonames.org/export/dump/{code}.zip", zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extract(f"{code}.txt", geonames_dir)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    return txt_path


def _sha256_and_write(path, data_bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data_bytes)
    os.replace(tmp, path)
    return len(data_bytes), hashlib.sha256(data_bytes).hexdigest()


def build_region_pack(
    reg,
    pbf_path,
    sampler,
    geonames_dir,
    out_assets_dir,
    prev_entry,
    osm_timestamp,
    release_base_url,
    work_dir,
):
    """Builds <rid>__trails.bin.gz, <rid>__pois.json.gz, <rid>__water.bin.gz and returns manifest entry."""
    rid = reg["id"]
    min_la = float(min(t[0] for t in reg["tiles"]))
    max_la = float(max(t[0] for t in reg["tiles"]) + 1)
    min_lo = float(min(t[1] for t in reg["tiles"]))
    max_lo = float(max(t[1] for t in reg["tiles"]) + 1)
    clip_pad = 0.15
    feats = []
    wfeats = []

    def within_region_bbox(pts):
        for la, lo in pts:
            if (min_la - clip_pad) <= la <= (max_la + clip_pad) and (min_lo - clip_pad) <= lo <= (max_lo + clip_pad):
                return True
        return False

    def run_tmp_export(tag_args, tag_name):
        fpbf = os.path.join(work_dir, f"_tmp_{rid}_{tag_name}.osm.pbf")
        gseq = os.path.join(work_dir, f"_tmp_{rid}_{tag_name}.geojsonseq")
        subprocess.run(["osmium", "tags-filter", "-O", pbf_path, *tag_args, "-o", fpbf], check=True)
        subprocess.run(["osmium", "export", "-O", "-f", "geojsonseq", fpbf, "-o", gseq], check=True)
        if os.path.exists(fpbf):
            os.remove(fpbf)
        return gseq

    def pick_name(props):
        return (props.get("name:en") or props.get("name") or "").strip()

    # 1. Marked hiking/foot/mtb routes
    gseq = run_tmp_export(["r/route=hiking,foot,mtb"], "routes")
    idx = 0
    for feat in iter_geojsonseq(gseq):
        geom, props = feat.get("geometry") or {}, feat.get("properties") or {}
        name = pick_name(props)[:50]
        ref = (props.get("ref") or "")[:15]
        for line in extract_rings(geom):
            if len(line) < 2:
                continue
            raw_pts = [(c[1], c[0]) for c in line]
            if not within_region_bbox(raw_pts):
                continue
            simp = rdp_simplify(raw_pts, 0.00008)
            if len(simp) >= 2:
                idx += 1
                feats.append(
                    {"id": f"{rid}_rt_{idx}", "name": name, "ref": ref, "is_marked": True, "difficulty": 1, "kind": 0, "points": simp}
                )
    os.remove(gseq)

    # 2. Highways / roads / tracks / paths
    gseq = run_tmp_export(["w/highway"], "hw")
    idx = 0
    for feat in iter_geojsonseq(gseq):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        props = feat.get("properties") or {}
        hw = props.get("highway")
        if hw == "service" and props.get("service") in ("parking_aisle", "driveway", "drive-through"):
            continue
        tunnel = props.get("tunnel") == "yes"
        if hw in ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link"):
            kind, eps = (4 if tunnel else 3), 0.00008
        elif hw in ("tertiary", "tertiary_link", "unclassified", "road"):
            kind, eps = 2, 0.00010
        elif hw in ("residential", "living_street", "service"):
            kind, eps = 2, 0.00012
        elif hw == "track":
            kind, eps = 1, 0.00010
        elif hw in ("path", "footway", "bridleway", "steps", "pedestrian"):
            kind, eps = 0, 0.00008
        else:
            continue
        raw_pts = [(c[1], c[0]) for c in coords]
        if not within_region_bbox(raw_pts):
            continue
        simp = rdp_simplify(raw_pts, eps)
        if len(simp) >= 2:
            idx += 1
            feats.append(
                {
                    "id": f"{rid}_hw_{idx}",
                    "name": pick_name(props)[:50],
                    "ref": (props.get("ref") or "")[:15],
                    "is_marked": False,
                    "difficulty": 0,
                    "kind": kind,
                    "points": simp,
                }
            )
    os.remove(gseq)

    # 3. Waterways & water polygons
    gseq = run_tmp_export(
        ["w/waterway=river,stream", "nwr/natural=water", "nwr/waterway=riverbank", "nwr/landuse=reservoir"],
        "water",
    )
    idx = 0
    for feat in iter_geojsonseq(gseq):
        geom, props = feat.get("geometry") or {}, feat.get("properties") or {}
        ww, nat, lu = props.get("waterway"), props.get("natural"), props.get("landuse")
        name = pick_name(props)[:50]
        gtype = geom.get("type")
        if ww in ("river", "stream") and gtype in ("LineString", "MultiLineString"):
            kind = 7 if ww == "river" else 8
            eps = 0.00018 if ww == "river" else 0.00015
            for line in extract_rings(geom):
                if len(line) < 2:
                    continue
                raw_pts = [(c[1], c[0]) for c in line]
                if not within_region_bbox(raw_pts):
                    continue
                simp = rdp_simplify(raw_pts, eps)
                if len(simp) >= 2:
                    idx += 1
                    rec = {"id": f"{rid}_rv_{idx}", "name": name, "ref": "", "is_marked": False, "difficulty": 0, "kind": kind, "points": simp}
                    feats.append(rec)
                    if kind == 7:
                        wfeats.append(rec)
        elif (nat == "water" or ww == "riverbank" or lu == "reservoir") and gtype in ("Polygon", "MultiPolygon"):
            kind = 12 if ww == "riverbank" else 9
            for ring in extract_rings(geom):
                if len(ring) < 4:
                    continue
                raw_pts = [(c[1], c[0]) for c in ring]
                if not within_region_bbox(raw_pts):
                    continue
                simp = rdp_simplify(raw_pts, 0.00018)
                if len(simp) >= 4:
                    if simp[0] != simp[-1]:
                        simp.append(simp[0])
                    idx += 1
                    rec = {"id": f"{rid}_lk_{idx}", "name": name, "ref": "", "is_marked": False, "difficulty": 0, "kind": kind, "points": simp}
                    feats.append(rec)
                    wfeats.append(rec)
    os.remove(gseq)

    # 4. Landcover & coastline
    gseq = run_tmp_export(
        [
            "nwr/natural=wood,scrub,heath,bare_rock,scree,shingle,grassland,beach,sand,wetland,glacier",
            "nwr/landuse=forest,residential,commercial,retail,industrial,meadow,grass,village_green,farmland,orchard,vineyard",
            "nwr/leisure=park,nature_reserve",
            "nwr/boundary=protected_area,national_park",
            "w/natural=coastline",
        ],
        "lc",
    )
    idx = 0
    for feat in iter_geojsonseq(gseq):
        geom, props = feat.get("geometry") or {}, feat.get("properties") or {}
        nat, lu, lei, bnd = props.get("natural"), props.get("landuse"), props.get("leisure"), props.get("boundary")
        gtype = geom.get("type")
        name = pick_name(props)[:50]
        if nat == "coastline" and gtype in ("LineString", "MultiLineString"):
            for line in extract_rings(geom):
                if len(line) < 2:
                    continue
                raw_pts = [(c[1], c[0]) for c in line]
                if not within_region_bbox(raw_pts):
                    continue
                simp = rdp_simplify(raw_pts, 0.00008)
                if len(simp) >= 2:
                    idx += 1
                    feats.append(
                        {"id": f"{rid}_coast_{idx}", "name": "", "ref": "", "is_marked": False, "difficulty": 0, "kind": 11, "points": simp}
                    )
            continue
        if gtype not in ("Polygon", "MultiPolygon"):
            continue
        if nat == "wood" or lu == "forest":
            kind, eps = 13, 0.00030
        elif bnd in ("protected_area", "national_park") or lei == "nature_reserve":
            kind, eps = 14, 0.00035
        elif lu in ("residential", "commercial", "retail", "industrial"):
            kind, eps = 16, 0.00022
        elif nat in ("scrub", "heath"):
            kind, eps = 17, 0.00028
        elif nat in ("bare_rock", "scree", "shingle", "glacier"):
            kind, eps = 18, 0.00028
        elif nat == "grassland" or lu in ("meadow", "grass", "village_green") or lei == "park":
            kind, eps = 19, 0.00028
        elif nat in ("beach", "sand"):
            kind, eps = 20, 0.00022
        elif lu in ("farmland", "orchard", "vineyard"):
            kind, eps = 21, 0.00030
        elif nat == "wetland":
            kind, eps = 22, 0.00030
        else:
            continue
        for ring in extract_rings(geom):
            if len(ring) < 4:
                continue
            pts = [(c[1], c[0]) for c in ring]
            if not within_region_bbox(pts):
                continue
            la_min, la_max = min(p[0] for p in pts), max(p[0] for p in pts)
            lo_min, lo_max = min(p[1] for p in pts), max(p[1] for p in pts)
            if max(la_max - la_min, lo_max - lo_min) < 0.0006:
                continue
            simp = rdp_simplify(pts, eps)
            if len(simp) >= 4:
                if simp[0] != simp[-1]:
                    simp.append(simp[0])
                idx += 1
                feats.append(
                    {"id": f"{rid}_lc_{idx}", "name": name if kind == 14 else "", "ref": "", "is_marked": False, "difficulty": 0, "kind": kind, "points": simp}
                )
    os.remove(gseq)

    # 5. POIs: curated + GeoNames + OSM
    tile_set = set(reg["tiles"])
    buf_deg, min_buf_ele = 1.05, 1400.0
    candidates = []
    for pid, name, lat, lon, ele, ptype, prom in CURATED_PEAKS:
        in_core = (int(math.floor(lat)), int(math.floor(lon))) in tile_set
        in_buf = (
            ptype == "PEAK"
            and ele >= min_buf_ele
            and (min_la - buf_deg) <= lat <= (max_la + buf_deg)
            and (min_lo - buf_deg) <= lon <= (max_lo + buf_deg)
        )
        if in_core or in_buf:
            candidates.append({"id": pid, "name": name, "lat": lat, "lon": lon, "elevation": ele, "type": ptype, "prominence": prom})

    for gcode in reg["geonames"]:
        gtxt = ensure_geonames(gcode, geonames_dir)
        with open(gtxt, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 17:
                    continue
                lat, lon = float(parts[4]), float(parts[5])
                in_core = (int(math.floor(lat)), int(math.floor(lon))) in tile_set
                in_buf_box = (min_la - buf_deg) <= lat <= (max_la + buf_deg) and (min_lo - buf_deg) <= lon <= (max_lo + buf_deg)
                if not in_core and not in_buf_box:
                    continue
                gid, name = parts[0], parts[1].strip()
                if not name:
                    continue
                fclass, fcode = parts[6], parts[7]
                pop = int(parts[14]) if parts[14].isdigit() else 0
                elev_str, dem_str = parts[15].strip(), parts[16].strip()
                raw_ele = (
                    float(elev_str)
                    if elev_str and elev_str.lstrip("-").isdigit()
                    else (
                        float(dem_str)
                        if dem_str and dem_str.lstrip("-").isdigit() and float(dem_str) > -500
                        else max(2.0, round(sampler.sample_elev(lat, lon)))
                    )
                )
                ele = max(2.0, min(9000.0, raw_ele))
                ptype, prom = None, 0.45
                if fclass == "P" and in_core:
                    if fcode in ("PPLC", "PPLA"):
                        ptype, prom = "SETTLEMENT", 0.85 if pop >= 300_000 else 0.78
                    elif fcode == "PPLA2":
                        ptype, prom = "SETTLEMENT", 0.66 if pop >= 35_000 else 0.60
                    elif fcode == "PPLA3":
                        ptype, prom = "SETTLEMENT", 0.52
                    elif fcode == "PPL":
                        ptype, prom = "SETTLEMENT", 0.54 if pop >= 5_000 else (0.44 if pop >= 500 else 0.36)
                elif fclass == "T":
                    if fcode in ("MT", "PK", "VLC", "HLL", "PT"):
                        ele = max(ele, round(sampler.sample_elev(lat, lon)))
                        min_req = 120.0 if fcode in ("MT", "PK", "VLC") else 350.0
                        if not in_core:
                            min_req = min_buf_ele
                        if ele >= min_req:
                            ptype, prom = "PEAK", min(0.95, 0.42 + (ele / 8848.0) * 0.50)
                    elif fcode in ("PASS", "SDL") and in_core:
                        ptype, prom = "SADDLE", min(0.74, 0.46 + (ele / 5500.0) * 0.24)
                elif fclass == "S" and in_core:
                    if fcode in ("ANS", "CSTL", "RUIN", "MNMT", "FT"):
                        ptype, prom = "RUINS", 0.56
                    elif fcode == "CAVE":
                        ptype, prom = "CAVE", 0.48
                    elif fcode in ("CMP", "HUT", "SHLT"):
                        ptype, prom = "CAMP", 0.46
                elif fclass == "H" and in_core and fcode in ("FLLS", "WTRW", "SPNG", "WLL"):
                    ptype, prom = "WATERFALL", 0.50 if fcode in ("FLLS", "WTRW") else 0.42
                if ptype:
                    candidates.append(
                        {
                            "id": f"rg_{rid}_{gid}",
                            "name": name,
                            "lat": round(lat, 5),
                            "lon": round(lon, 5),
                            "elevation": float(round(ele)),
                            "type": ptype,
                            "prominence": round(prom, 3),
                        }
                    )

    gseq = run_tmp_export(
        [
            "n/natural=peak,volcano,spring,cave_entrance",
            "n/mountain_pass=yes",
            "n/waterway=waterfall",
            "nwr/tourism=camp_site,alpine_hut,wilderness_hut,viewpoint",
            "nwr/historic=ruins,castle,monastery,fort",
        ],
        "pois",
    )
    osm_idx = 0
    for feat in iter_geojsonseq(gseq):
        geom = feat.get("geometry") or {}
        gtype, coords = geom.get("type"), geom.get("coordinates") or []
        if gtype == "Point" and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
        elif gtype in ("Polygon", "MultiPolygon"):
            rings = extract_rings(geom)
            if not rings or not rings[0]:
                continue
            lat = sum(c[1] for c in rings[0]) / len(rings[0])
            lon = sum(c[0] for c in rings[0]) / len(rings[0])
        else:
            continue
        if (int(math.floor(lat)), int(math.floor(lon))) not in tile_set:
            continue
        props = feat.get("properties") or {}
        name = pick_name(props)
        if not name:
            continue
        nat, tour, hist = props.get("natural"), props.get("tourism"), props.get("historic")
        mpass, ww = props.get("mountain_pass"), props.get("waterway")
        ele = None
        if props.get("ele"):
            try:
                ele = float(str(props["ele"]).split(";")[0].replace("m", "").strip())
            except Exception:
                ele = None
        if ele is None:
            ele = max(2.0, round(sampler.sample_elev(lat, lon)))
        ptype, prom = None, 0.45
        if nat in ("peak", "volcano"):
            ptype, prom = "PEAK", min(0.95, 0.44 + (ele / 8848.0) * 0.48)
        elif mpass == "yes":
            ptype, prom = "SADDLE", min(0.75, 0.48 + (ele / 5500.0) * 0.24)
        elif tour in ("camp_site", "alpine_hut", "wilderness_hut"):
            ptype, prom = "CAMP", 0.56 if "hut" in tour else 0.48
        elif tour == "viewpoint":
            ptype, prom = "PEAK", 0.44
        elif hist in ("ruins", "castle", "monastery", "fort"):
            ptype, prom = "RUINS", 0.56
        elif ww == "waterfall" or nat == "spring":
            ptype, prom = "WATERFALL", 0.52 if ww == "waterfall" else 0.42
        elif nat == "cave_entrance":
            ptype, prom = "CAVE", 0.48
        if not ptype:
            continue
        osm_idx += 1
        candidates.append(
            {
                "id": f"{rid}_osm_{osm_idx}",
                "name": name[:60],
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "elevation": float(round(max(2.0, min(9000.0, ele)))),
                "type": ptype,
                "prominence": round(prom, 3),
            }
        )
    os.remove(gseq)

    candidates.sort(key=lambda p: (-p["prominence"], -p["elevation"]))
    deduped, grid = [], {}
    for p in candidates:
        key_cell = (p["type"], round(p["lat"] * 120), round(p["lon"] * 120), p["name"].lower()[:12])
        if key_cell in grid:
            continue
        grid[key_cell] = True
        deduped.append(p)
        if len(deduped) >= 8000:
            break

    raw_pois = json.dumps({"pois": deduped}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz_pois = gzip.compress(raw_pois, compresslevel=9)
    p_sz, p_sha = _sha256_and_write(os.path.join(out_assets_dir, f"{rid}__pois.json.gz"), gz_pois)

    raw_trails = encode_ktr1(feats)
    gz_trails = gzip.compress(raw_trails, compresslevel=9)
    t_sz, t_sha = _sha256_and_write(os.path.join(out_assets_dir, f"{rid}__trails.bin.gz"), gz_trails)

    raw_water = encode_ktr1(wfeats)
    gz_water = gzip.compress(raw_water, compresslevel=9)
    w_sz, w_sha = _sha256_and_write(os.path.join(out_assets_dir, f"{rid}__water.bin.gz"), gz_water)

    print(
        f"Region {rid}: {len(deduped)} POIs ({p_sz/1024:.0f} KB), "
        f"{len(feats)} trails/roads/lc ({t_sz/1024/1024:.2f} MB), "
        f"{len(wfeats)} water ({w_sz/1024:.0f} KB)",
        flush=True,
    )

    if os.path.exists(pbf_path):
        os.remove(pbf_path)

    next_ver = int((prev_entry or {}).get("version", 0)) + 1
    rel_base = release_base_url.rstrip("/")
    entry = {
        "id": rid,
        "version": next_ver,
        "osm_timestamp": osm_timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "name_en": reg["name_en"],
        "name_ru": reg["name_ru"],
        "name_tr": reg["name_tr"],
        "bbox": [min_la, min_lo, max_la, max_lo],
        "tiles": [f"N{la:02d}E{lo:03d}.hgt" for la, lo in sorted(reg["tiles"])],
        "horizon_tiles": [f"N{la:02d}E{lo:03d}.hgt" for la, lo in sorted(reg.get("horizon_tiles", []))],
        "files": {
            "trails.bin.gz": {"url": f"{rel_base}/{rid}__trails.bin.gz", "size": t_sz, "raw_size": len(raw_trails), "sha256": t_sha},
            "pois.json.gz": {"url": f"{rel_base}/{rid}__pois.json.gz", "size": p_sz, "raw_size": len(raw_pois), "sha256": p_sha},
            "water.bin.gz": {"url": f"{rel_base}/{rid}__water.bin.gz", "size": w_sz, "raw_size": len(raw_water), "sha256": w_sha},
        },
    }
    return entry

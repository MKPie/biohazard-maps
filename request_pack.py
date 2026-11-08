#!/usr/bin/env python3
"""Builds a map pack for one requested place. Run by the GitHub workflow when the relay asks for a place.
Picks the smallest Geofabrik extracts that cover the 50-mile circle (more than one when it crosses a state line), downloads them, and runs mkpack.py.
  python3 request_pack.py --id evanston-il --name "Evanston, Illinois" --lat 42.045 --lon -87.688 --zips 602 --places evanston
"""
import argparse, concurrent.futures, json, math, os, re, subprocess, sys, time, urllib.error, urllib.request

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
RADIUS_MILES = 50     # everything within 50 miles of the city center goes into the pack
RADIUS_KM = RADIUS_MILES * 1.609344


def inside(x, y, ring):
    ins = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            ins = not ins
        j = i
    return ins


def polys(geom):
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    return []


def contains(geom, lon, lat):
    for poly in polys(geom):
        if inside(lon, lat, poly[0]) and not any(inside(lon, lat, h) for h in poly[1:]):
            return True
    return False


def area(geom):
    """Rough planar area of the outer rings in square degrees: enough to tell a state from a continent."""
    t = 0.0
    for poly in polys(geom):
        r = poly[0]
        t += abs(sum(r[i][0] * r[(i + 1) % len(r)][1] - r[(i + 1) % len(r)][0] * r[i][1] for i in range(len(r)))) / 2.0
    return t


def pick_extract(index, lon, lat):
    """(pbf url, extract id) of the smallest extract that has a pbf and covers the point, or None."""
    best = None
    for f in index["features"]:
        urls = (f.get("properties") or {}).get("urls") or {}
        pbf = urls.get("pbf")
        g = f.get("geometry")
        if not pbf or not g or not contains(g, lon, lat):
            continue
        a = area(g)
        if best is None or a < best[0]:
            best = (a, pbf, f["properties"].get("id"))
    return None if best is None else (best[1], best[2])


def sample_points(lat, lon, radius_km):
    """The center plus rings of points at half and full radius: every state or country the circle touches holds at least one."""
    pts = [(lon, lat)]
    for frac in (0.5, 1.0):
        for k in range(8):
            ang = math.radians(45 * k)
            pts.append((lon + frac * radius_km * math.sin(ang) / (111.320 * max(0.05, math.cos(math.radians(lat)))),
                        lat + frac * radius_km * math.cos(ang) / 110.574))
    return [(x, y) for x, y in pts]


def pick_extracts(index, lon, lat, radius_km):
    """[(pbf url, extract id)]: the smallest extract that covers the center and each sample point, no repeats, center's first.
    Only the center may use a whole-region or whole-country file (id without a slash, like us-midwest). Points out on a lake or
    over a border would otherwise pull in a multi-gigabyte regional file just to cover water, which cost a 9 minute download."""
    out, seen = [], set()
    for x, y in sample_points(lat, lon, radius_km):
        p = pick_extract(index, x, y)
        if p is None or p[1] in seen:
            continue
        if out and "/" not in (p[1] or ""):
            continue
        seen.add(p[1])
        out.append(p)
    return out


def _get(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "BioHazard-pack-builder"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest + ".part", "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    os.replace(dest + ".part", dest)


def fetch(url, repo=None):
    """Downloads one map file from Geofabrik, trying up to 3 times."""
    os.makedirs("extracts", exist_ok=True)
    name = os.path.basename(url)
    dest = "extracts/" + name
    if os.path.exists(dest):
        return dest
    t0 = time.time()
    for attempt in (1, 2, 3):
        try:
            _get(url, dest)
            mb = os.path.getsize(dest) / 1e6
            dt = max(0.001, time.time() - t0)
            print("downloaded %s: %.0f MB in %.0f s (%.1f MB/s)" % (name, mb, dt, mb / dt), flush=True)
            return dest
        except Exception as e:
            print("%s try %d failed: %s" % (name, attempt, e), flush=True)
            time.sleep(3 * attempt)
    sys.exit("could not download " + name)


def fetch_all(picks, repo):
    """All the files at once, so the slowest one decides the time instead of the sum."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(picks))) as ex:
        return list(ex.map(lambda p: fetch(p[0], repo), picks))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--zips", default="")
    ap.add_argument("--places", default="")
    ap.add_argument("--index-file", default="", help="use this index-v1.json instead of downloading it (tests)")
    ap.add_argument("--extract-file", default="", help="use this map file instead of downloading one (tests)")
    ap.add_argument("--print-extract", action="store_true", help="only print the map file name this place needs, then stop (the workflow caches it)")
    ap.add_argument("--download-only", action="store_true", help="download the map files this place needs, then stop (its own timed step in the workflow)")
    ap.add_argument("--repo", default="MKPie/biohazard-maps")
    ap.add_argument("--out", default="packs")
    a = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]{2,40}", a.id):
        sys.exit("bad id")
    if not (-90 <= a.lat <= 90 and -180 <= a.lon <= 180):
        sys.exit("bad coordinates")
    if a.extract_file:
        srcs = [a.extract_file]
    else:
        if a.index_file:
            index = json.load(open(a.index_file, encoding="utf-8"))
        else:
            req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "BioHazard-pack-builder"})
            index = json.load(urllib.request.urlopen(req, timeout=120))
        picks = pick_extracts(index, a.lon, a.lat, RADIUS_KM)
        if not picks:
            sys.exit("No map extract covers %.4f, %.4f" % (a.lat, a.lon))
        if a.print_extract:
            print("+".join(sorted(p[1].replace("/", "_") for p in picks)))
            return
        print("map extracts: " + ", ".join(p[1] for p in picks), flush=True)
        srcs = fetch_all(picks, a.repo)
        if a.download_only:
            return
    cmd = [sys.executable, "mkpack.py", "--pbf"] + srcs + ["--id", a.id, "--name", a.name, "--lat", str(a.lat), "--lon=" + str(a.lon),
           "--radius-km", "%.3f" % RADIUS_KM, "--zips", a.zips, "--places", a.places, "--repo", a.repo, "--out", a.out]
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()

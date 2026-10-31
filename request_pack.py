#!/usr/bin/env python3
"""Builds a map pack for one requested place. Run by the GitHub workflow when the relay asks for a place.
Picks the smallest Geofabrik extract that covers the point, downloads it, and runs mkpack.py.
  python3 request_pack.py --id evanston-il --name "Evanston, Illinois" --lat 42.045 --lon -87.688 --zips 602 --places evanston
"""
import argparse, json, math, os, re, subprocess, sys, urllib.request

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
HALF_DEG_LAT = 0.22   # about 24 km each way


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
    ap.add_argument("--repo", default="MKPie/biohazard-maps")
    ap.add_argument("--out", default="packs")
    a = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]{2,40}", a.id):
        sys.exit("bad id")
    if not (-90 <= a.lat <= 90 and -180 <= a.lon <= 180):
        sys.exit("bad coordinates")
    if a.extract_file:
        src = a.extract_file
    else:
        if a.index_file:
            index = json.load(open(a.index_file, encoding="utf-8"))
        else:
            req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "BioHazard-pack-builder"})
            index = json.load(urllib.request.urlopen(req, timeout=120))
        pick = pick_extract(index, a.lon, a.lat)
        if pick is None:
            sys.exit("No map extract covers %.4f, %.4f" % (a.lat, a.lon))
        url, eid = pick
        os.makedirs("extracts", exist_ok=True)
        src = "extracts/" + os.path.basename(url)
        if not os.path.exists(src):
            print("downloading " + url)
            req = urllib.request.Request(url, headers={"User-Agent": "BioHazard-pack-builder"})
            with urllib.request.urlopen(req, timeout=600) as r, open(src, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
    dlat = HALF_DEG_LAT
    dlon = HALF_DEG_LAT / max(0.2, math.cos(math.radians(a.lat)))
    bbox = "%.4f,%.4f,%.4f,%.4f" % (a.lon - dlon, a.lat - dlat, a.lon + dlon, a.lat + dlat)
    cmd = [sys.executable, "mkpack.py", "--pbf", src, "--id", a.id, "--name", a.name, "--lat", str(a.lat), "--lon=" + str(a.lon),
           "--bbox=" + bbox, "--zips", a.zips, "--places", a.places, "--repo", a.repo, "--out", a.out]
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()

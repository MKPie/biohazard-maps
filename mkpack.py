#!/usr/bin/env python3
"""Builds one Bio Hazard map pack from an OpenStreetMap extract (.osm.pbf or .osm).

  pip install osmium            (once)
  python3 mkpack.py --pbf illinois-latest.osm.pbf --id evanston --name Evanston \
      --lat 42.0451 --lon -87.6877 --bbox=-87.80,41.97,-87.60,42.10 \
      --zips 600,602 --places evanston,skokie,wilmette

Writes <out>/<id>.zip (meta.tsv, streets.tsv, areas.tsv) and adds/updates the line for <id> in <out>/regions.tsv.
Upload <id>.zip as a GitHub Release file and commit regions.tsv to the repo (see the printed reminder).
streets.tsv: one line per intersection of two differently named streets: "<street a>|<street b>" TAB lat TAB lon,
  names normalized exactly like the app does (AddressLookup.norm).
areas.tsv: one area per line: name TAB ring ("lon,lat lon,lat ...") [TAB ring ...].
"""
import argparse, hashlib, os, re, sys, zipfile
import osmium

SUFFIX = {"avenue": "ave", "av": "ave", "ave": "ave", "street": "st", "str": "st", "st": "st", "road": "rd", "rd": "rd",
          "boulevard": "blvd", "blvd": "blvd", "drive": "dr", "dr": "dr", "court": "ct", "ct": "ct", "place": "pl", "pl": "pl",
          "lane": "ln", "ln": "ln", "terrace": "ter", "ter": "ter", "terr": "ter", "parkway": "pkwy", "pkwy": "pkwy",
          "highway": "hwy", "hwy": "hwy", "way": "way", "circle": "cir", "cir": "cir", "expressway": "expy", "expy": "expy",
          "trail": "trl", "trl": "trl", "square": "sq", "sq": "sq"}
DIRS = {"north": "n", "n": "n", "south": "s", "s": "s", "east": "e", "e": "e", "west": "w", "w": "w"}
IL_ROUTE = re.compile(r"(?:il|illinois)(?: state)?(?: route| rte| hwy| highway)? (\d+)$")
US_ROUTE = re.compile(r"(?:us|u s)(?: route| rte| hwy| highway)? (\d+)$")
DRIVABLE = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service",
            "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link", "living_street"}


def norm(name):
    """Same as AddressLookup.norm + normAddr in the app."""
    t = [w for w in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split() if w]
    out = []
    for i, w in enumerate(t):
        if i == 0 and w in DIRS and len(t) > 1:
            out.append(DIRS[w])
        elif i == len(t) - 1 and w in SUFFIX and len(t) > 1:
            out.append(SUFFIX[w])
        else:
            out.append(w)
    n = " ".join(out)
    m = IL_ROUTE.match(n) or US_ROUTE.match(n)
    if m:
        return ("il " if IL_ROUTE.match(n) else "us ") + m.group(1)
    return n


def key(a, b):
    return a + "|" + b if a <= b else b + "|" + a


class Streets(osmium.SimpleHandler):
    def __init__(self, bbox, levels):
        super().__init__()
        self.bbox = bbox
        self.levels = levels
        self.first = {}      # node ref -> (name, lat, lon)
        self.multi = {}      # node ref -> set of names (only nodes where 2+ names meet)
        self.areas = {}      # area name -> list of rings
        self.ways = 0

    def way(self, w):
        if w.tags.get("highway") not in DRIVABLE:
            return
        name = w.tags.get("name")
        if not name:
            return
        n = norm(name)
        if not n:
            return
        self.ways += 1
        x0, y0, x1, y1 = self.bbox
        for nd in w.nodes:
            if not nd.location.valid():
                continue
            lat, lon = nd.location.lat, nd.location.lon
            if lon < x0 or lon > x1 or lat < y0 or lat > y1:
                continue
            f = self.first.get(nd.ref)
            if f is None:
                self.first[nd.ref] = (n, lat, lon)
            elif f[0] != n:
                self.multi.setdefault(nd.ref, {f[0]}).add(n)

    def area(self, a):
        t = a.tags
        name = t.get("name")
        if not name:
            return
        lvl = t.get("admin_level")
        ok = (t.get("boundary") == "administrative" and lvl in self.levels) or t.get("place") in ("neighbourhood", "suburb", "quarter")
        if not ok:
            return
        x0, y0, x1, y1 = self.bbox
        for ring in a.outer_rings():
            pts = [(n.lon, n.lat) for n in ring if n.location.valid()]
            if len(pts) < 4:
                continue
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            if cx < x0 or cx > x1 or cy < y0 or cy > y1:
                continue
            self.areas.setdefault(name, []).append(pts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--bbox", required=True, help="minlon,minlat,maxlon,maxlat")
    ap.add_argument("--zips", default="", help="comma separated 3-digit zip prefixes the pack covers")
    ap.add_argument("--places", default="", help="comma separated city names the pack covers")
    ap.add_argument("--center-name", default="")
    ap.add_argument("--levels", default="9,10", help="admin_level values to treat as neighborhoods")
    ap.add_argument("--grid", default="none", choices=["none", "chicago"])
    ap.add_argument("--repo", default="MKPie/biohazard-maps")
    ap.add_argument("--tag", default="packs", help="GitHub release tag the zip is uploaded to")
    ap.add_argument("--out", default="packs")
    a = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9_-]{2,40}", a.id) or a.id in ("chicago", "none"):
        sys.exit("--id must be 2-40 characters of a-z 0-9 _ - and not 'chicago' or 'none'")
    bbox = [float(v) for v in a.bbox.split(",")]
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        sys.exit("--bbox must be minlon,minlat,maxlon,maxlat")
    zips = [z.strip() for z in a.zips.split(",") if z.strip()]
    if any(not re.fullmatch(r"\d{3}", z) for z in zips):
        sys.exit("--zips must be 3-digit prefixes like 600,602")
    places = [p.strip().lower() for p in a.places.split(",") if p.strip()]

    h = Streets(bbox, set(a.levels.split(",")))
    # node locations go to a file on disk so a whole state extract does not need gigabytes of memory
    idx = "sparse_file_array," + os.path.join(os.path.dirname(os.path.abspath(a.out)) if False else a.out, ".nodeidx")
    os.makedirs(a.out, exist_ok=True)
    try:
        h.apply_file(a.pbf, locations=True, idx=idx)
    finally:
        try:
            os.remove(os.path.join(a.out, ".nodeidx"))
        except OSError:
            pass

    rows = {}
    for ref, names in h.multi.items():
        _, lat, lon = h.first[ref]
        ns = sorted(names)
        for i in range(len(ns)):
            for j in range(i + 1, len(ns)):
                rows.setdefault(key(ns[i], ns[j]), (lat, lon))
    if not rows:
        sys.exit("No intersections found inside the bbox. Check the bbox and that the extract covers it.")

    os.makedirs(a.out, exist_ok=True)
    streets = "".join("%s\t%.6f\t%.6f\n" % (k, v[0], v[1]) for k, v in sorted(rows.items()))
    areas = ""
    for nm, rings in sorted(h.areas.items()):
        areas += nm.replace("\t", " ") + "".join("\t" + " ".join("%.6f,%.6f" % p for p in r) for r in rings) + "\n"
    center = a.center_name or a.name
    meta = "id\t%s\nname\t%s\ngrid\t%s\nlat\t%s\nlon\t%s\ncenter_name\t%s\n" % (a.id, a.name, a.grid, a.lat, a.lon, center)
    zpath = os.path.join(a.out, a.id + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.tsv", meta)
        z.writestr("streets.tsv", streets)
        if areas:
            z.writestr("areas.tsv", areas)
    data = open(zpath, "rb").read()
    sha = hashlib.sha256(data).hexdigest()
    url = "https://github.com/%s/releases/download/%s/%s.zip" % (a.repo, a.tag, a.id)
    line = "\t".join([a.id, a.name, a.grid, str(a.lat), str(a.lon), center, str(len(data)), sha, url, ",".join(zips), ",".join(places)])
    ipath = os.path.join(a.out, "regions.tsv")
    old = []
    if os.path.exists(ipath):
        old = [l for l in open(ipath, encoding="utf-8").read().splitlines() if l.strip() and not l.startswith(a.id + "\t")]
    if not any(l.startswith("#") for l in old):
        old.insert(0, "# id, name, grid, lat, lon, center_name, bytes, sha256, url, zip prefixes, places")
    with open(ipath, "w", encoding="utf-8") as f:
        f.write("\n".join(old + [line]) + "\n")
    print("ways used: %d | intersections: %d | areas: %d" % (h.ways, len(rows), len(h.areas)))
    print("wrote %s (%.1f MB) and updated %s" % (zpath, len(data) / 1048576.0, ipath))
    print("next: upload %s as a Release file on %s with tag '%s', then commit and push %s to the repo root." % (zpath, a.repo, a.tag, ipath))


if __name__ == "__main__":
    main()

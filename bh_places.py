#!/usr/bin/env python3
"""Pulls the places a rideshare driver goes to (airports, hospitals, restaurants, shops, gas stations, parks, schools, offices, hotels, train stations...)
out of OpenStreetMap files, inside one box. Writes places.tsv: name TAB category TAB lat TAB lon.

  python3 bh_places.py --bbox=-88.60,41.15,-86.65,42.60 --out chicago_places.tsv  file1.osm.pbf file2.osm.pbf

pip install osmium (once). Several files are fine (a metro can cross a state line); a place found twice is kept once.
"""
import argparse, math, os, shutil, subprocess, sys, tempfile
import osmium


def classify(t):
    """The category a tagged thing belongs to, or None when drivers would not look for it."""
    aero = t.get("aeroway")
    if aero == "aerodrome" and (t.get("iata") or t.get("aerodrome:type") == "public" or t.get("aerodrome") in ("international", "public")):
        return "Airport"
    if aero == "terminal":
        return "Airport terminal"
    amen = t.get("amenity")
    heal = t.get("healthcare")
    if amen == "hospital" or heal == "hospital":
        return "Hospital"
    if heal == "urgent_care" or (amen == "clinic" and "urgent" in (t.get("name") or "").lower()):
        return "Urgent care"
    if amen in ("clinic", "doctors", "dentist") or heal in ("clinic", "doctor", "dentist"):
        return "Clinic"
    if amen in ("university", "college"):
        return "College"
    if amen == "bus_station":
        return "Bus station"
    if amen == "casino":
        return "Casino"
    if amen in ("theatre", "arts_centre"):
        return "Theater"
    if amen in ("conference_centre", "exhibition_centre"):
        return "Convention center"
    if t.get("railway") == "station" and t.get("station") != "subway_entrance":
        return "Train station"
    if t.get("public_transport") == "station" and t.get("train") == "yes":
        return "Train station"
    if t.get("shop") == "mall":
        return "Mall"
    if t.get("leisure") == "stadium" or t.get("building") == "stadium":
        return "Stadium"
    tour = t.get("tourism")
    if tour in ("museum", "gallery"):
        return "Museum"
    if tour in ("zoo", "aquarium"):
        return "Zoo and aquarium"
    if tour == "theme_park":
        return "Theme park"
    if tour == "attraction":
        return "Attraction"
    if tour == "hotel":
        return "Hotel"
    if amen in ("restaurant", "food_court"):
        return "Restaurant"
    if amen == "fast_food":
        return "Fast food"
    if amen == "cafe":
        return "Cafe"
    if amen in ("bar", "pub", "nightclub", "biergarten"):
        return "Bar"
    if amen == "fuel":
        return "Gas station"
    if amen in ("school", "kindergarten"):
        return "School"
    if amen == "place_of_worship":
        return "Place of worship"
    if amen == "grave_yard" or t.get("landuse") == "cemetery":
        return "Cemetery"
    lei = t.get("leisure")
    if lei in ("park", "nature_reserve", "garden"):
        return "Park"
    if lei == "golf_course":
        return "Golf course"
    if lei == "marina":
        return "Marina"
    if t.get("natural") == "beach":
        return "Beach"
    if t.get("building") == "apartments":
        return "Apartments"
    if t.get("office") or t.get("building") == "office":
        return "Office"
    if t.get("shop"):
        return "Shop"
    return None


class Places(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.bbox = bbox
        self.found = []          # (name, category, lat, lon)

    def _add(self, tags, lat, lon):
        name = tags.get("name")
        if not name:
            return
        cat = classify(tags)
        if cat is None:
            return
        x0, y0, x1, y1 = self.bbox
        if lon < x0 or lon > x1 or lat < y0 or lat > y1:
            return
        self.found.append((name.strip(), cat, lat, lon))

    def node(self, n):
        if n.location.valid():
            self._add(n.tags, n.location.lat, n.location.lon)

    def area(self, a):
        t = a.tags
        if not t.get("name") or classify(t) is None:
            return
        xs, ys = [], []
        for ring in a.outer_rings():
            for p in ring:
                if p.location.valid():
                    xs.append(p.lon)
                    ys.append(p.lat)
        if xs:
            self._add(t, (min(ys) + max(ys)) / 2.0, (min(xs) + max(xs)) / 2.0)


def meters(a, b, c, d):
    r = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = p2 - p1, math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


BIG = {"Airport", "Airport terminal", "Hospital", "College", "Bus station", "Casino", "Theater", "Convention center", "Train station",
       "Mall", "Stadium", "Museum", "Zoo and aquarium", "Theme park", "Attraction", "Park", "Golf course", "Cemetery", "Marina", "Beach"}


def dedupe(rows):
    """Same name and category close together is one place (a building and its entrance node, or two files).
    Big places count as one within 400 m; shops, restaurants and the like within 60 m (two real branches are never that close)."""
    out = []
    seen = {}
    for name, cat, lat, lon in rows:
        k = (name.lower(), cat)
        lim = 400.0 if cat in BIG else 60.0
        if any(meters(lat, lon, la, lo) < lim for la, lo in seen.get(k, [])):
            continue
        seen.setdefault(k, []).append((lat, lon))
        out.append((name, cat, lat, lon))
    return out


POI_FILTERS = [
    "nwr/amenity=hospital,university,college,bus_station,casino,theatre,arts_centre,conference_centre,exhibition_centre,"
    "clinic,doctors,dentist,restaurant,food_court,fast_food,cafe,bar,pub,nightclub,biergarten,fuel,school,kindergarten,place_of_worship,grave_yard",
    "nwr/healthcare=hospital,urgent_care,clinic,doctor,dentist", "nwr/aeroway=aerodrome,terminal", "nwr/railway=station", "nwr/public_transport=station",
    "nwr/shop", "nwr/leisure=stadium,park,nature_reserve,garden,golf_course,marina", "nwr/natural=beach", "nwr/landuse=cemetery",
    "nwr/building=stadium,apartments,office", "nwr/office",
    "nwr/tourism=museum,gallery,zoo,aquarium,theme_park,attraction,hotel",
]


def prefilter(files, bbox, work):
    """With the osmium command line tool, cut each big extract to the box and keep only the place tags, so Python reads a small file."""
    if shutil.which("osmium") is None:
        print("note: the osmium tool is not installed, reading the big files as they are (slower; 'sudo apt install osmium-tool' makes it fast)")
        return list(files)
    box = "%f,%f,%f,%f" % tuple(bbox)
    out = []
    for i, f in enumerate(files):
        clip = os.path.join(work, "clip%d.osm.pbf" % i)
        kept = os.path.join(work, "kept%d.osm.pbf" % i)
        subprocess.run(["osmium", "extract", "-b", box, "--strategy=smart", "-S", "types=multipolygon,boundary", "--overwrite", "-o", clip, f], check=True)
        subprocess.run(["osmium", "tags-filter", "--overwrite", "-o", kept, clip] + POI_FILTERS, check=True)
        out.append(kept)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", required=True, help="minlon,minlat,maxlon,maxlat")
    ap.add_argument("--out", required=True)
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    bbox = [float(v) for v in a.bbox.split(",")]
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        sys.exit("--bbox must be minlon,minlat,maxlon,maxlat")
    rows = []
    work = tempfile.mkdtemp(prefix="bhplaces")
    try:
        for f in prefilter(a.files, bbox, work):
            h = Places(bbox)
            h.apply_file(f, locations=True)
            print("%s: %d places" % (os.path.basename(f), len(h.found)))
            rows += h.found
    finally:
        shutil.rmtree(work, ignore_errors=True)
    rows = dedupe(rows)
    rows.sort(key=lambda r: (r[1], r[0].lower()))
    with open(a.out, "w", encoding="utf-8") as fh:
        for name, cat, lat, lon in rows:
            fh.write("%s\t%s\t%.5f\t%.5f\n" % (name.replace("\t", " ").replace("\n", " "), cat, lat, lon))
    counts = {}
    for r in rows:
        counts[r[1]] = counts.get(r[1], 0) + 1
    print("wrote %d places to %s: %s" % (len(rows), a.out, ", ".join("%s %d" % (k, counts[k]) for k in sorted(counts))))


if __name__ == "__main__":
    main()

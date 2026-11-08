#!/usr/bin/env python3
"""Prints the list of US map files to copy into our own GitHub release, as the JSON the workflow matrix reads.
Only the smallest files (a state, or a part of a state when Geofabrik splits it); never the whole-country or region files."""
import json, sys, time, urllib.request

INDEX_URL = "https://download.geofabrik.de/index-v1.json"


def pick(index):
    feats = [f for f in index["features"] if f.get("properties")]
    by_id = {f["properties"]["id"]: f["properties"] for f in feats}
    parents = {p.get("parent") for p in by_id.values() if p.get("parent") and (p.get("urls") or {}).get("pbf")}

    def in_us(pid):
        seen = 0
        while pid and seen < 10:
            if pid == "us":
                return True
            pid = (by_id.get(pid) or {}).get("parent")
            seen += 1
        return False

    out = []
    for pid, p in sorted(by_id.items()):
        pbf = (p.get("urls") or {}).get("pbf")
        if not pbf or pid == "us" or pid.startswith("us-") or pid in parents or not in_us(pid):
            continue
        name = pbf.rsplit("/", 1)[-1]
        out.append({"id": pid, "file": name, "url": pbf})
    return out


def main():
    last = None
    for _ in range(4):
        try:
            req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "BioHazard-mirror"})
            index = json.load(urllib.request.urlopen(req, timeout=120))
            break
        except Exception as e:
            last = e
            time.sleep(5)
    else:
        sys.exit("could not read the Geofabrik index: %s" % last)
    items = pick(index)
    if len(items) < 40:
        sys.exit("only %d US files found; expected about 50. Not copying anything." % len(items))
    if "--lines" in sys.argv:
        for it in items:
            print(it["file"] + " " + it["url"])
    else:
        print(json.dumps({"include": items}, separators=(",", ":")))


if __name__ == "__main__":
    main()

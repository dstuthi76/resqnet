"""Download real shelters / hospitals / schools for the pilot area from OpenStreetMap
(Overpass API, free, no key) and save data/shelters.json. The backend and the
app use that file automatically instead of the demo shelter list.

    python data/fetch_osm.py
"""
import json
import os

import requests

BBOX = (17.28, 78.38, 17.43, 78.63)  # south, west, north, east - covers all ResQNet zones
QUERY = f"""
[out:json][timeout:60];
(
  node["amenity"~"hospital|clinic|school|community_centre|shelter"]({','.join(map(str, BBOX))});
  way["amenity"~"hospital|clinic|school|community_centre|shelter"]({','.join(map(str, BBOX))});
  node["emergency"="assembly_point"]({','.join(map(str, BBOX))});
);
out center tags;
"""


def main():
    r = requests.post("https://overpass-api.de/api/interpreter", data={"data": QUERY}, timeout=90)
    r.raise_for_status()
    out = []
    for e in r.json()["elements"]:
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lng = e.get("lon") or e.get("center", {}).get("lon")
        tags = e.get("tags", {})
        name = tags.get("name") or tags.get("name:en")
        if not (lat and lng and name):
            continue
        kind = tags.get("amenity") or tags.get("emergency")
        out.append({"name": name, "type": "hospital" if kind in ("hospital", "clinic") else "shelter",
                    "lat": round(lat, 6), "lng": round(lng, 6), "osm_id": e["id"]})
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shelters.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"saved {len(out)} places -> {path}  (data © OpenStreetMap contributors, ODbL)")


if __name__ == "__main__":
    main()

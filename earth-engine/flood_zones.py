"""ResQNet - flood duration per zone from Sentinel-1 radar, via the Earth Engine Python API.

Writes data/flood_readings.json, which the backend submits to the smart contract
("2 · Submit satellite readings" on the Relief page).

Setup (once):
    pip install earthengine-api
    earthengine authenticate            # opens a browser, sign in with your EE-registered Google account
Run:
    python earth-engine/flood_zones.py --project YOUR_CLOUD_PROJECT_ID

Same method as flood_zones.js - see the comments there.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
from zones import geojson  # noqa: E402

EVENT_ID = "HYD-2020-10"
BEFORE = ("2020-09-01", "2020-10-10")
AFTER = ("2020-10-12", "2020-11-10")
EVENT_START_MS = int(datetime(2020, 10, 13, 12, 30, tzinfo=timezone.utc).timestamp() * 1000)  # 18:00 IST
WATER_DB, DROP_DB, FRACTION_MIN = -16, 3, 0.05
DEFAULT_GAP_H = 6 * 24


def compute_readings(zone_ids, observations):
    """observations: list of {zoneId, t (ms), f (flooded fraction 0..1)} -> readings list."""
    by_zone = {}
    for o in observations:
        if o["f"] is None:
            continue
        by_zone.setdefault(o["zoneId"], []).append(o)
    readings = []
    for zid in zone_ids:
        obs = sorted(by_zone.get(zid, []), key=lambda x: x["t"])
        merged = []
        for x in obs:  # passes on the same day = adjacent tiles
            if merged and x["t"] - merged[-1]["t"] < 3 * 3600e3:
                merged[-1]["f"] = max(merged[-1]["f"], x["f"])
            else:
                merged.append({"t": x["t"], "f": x["f"]})
        flooded = [i for i, x in enumerate(merged) if x["f"] >= FRACTION_MIN]
        hours = 0
        if flooded:
            first, last = flooded[0], flooded[-1]
            half_gap = DEFAULT_GAP_H * 1800e3
            start = max(EVENT_START_MS, (merged[first - 1]["t"] + merged[first]["t"]) / 2 if first > 0
                        else merged[first]["t"] - half_gap)
            end = (merged[last]["t"] + merged[last + 1]["t"]) / 2 if last < len(merged) - 1 else merged[last]["t"] + half_gap
            hours = max(0, round((end - start) / 3600e3))
        readings.append({
            "zoneId": zid, "hoursFlooded": int(hours),
            "floodedFraction": round(max([x["f"] for x in merged], default=0), 3),
            "observations": [{"date": datetime.fromtimestamp(x["t"] / 1000, timezone.utc).isoformat(),
                              "fraction": round(x["f"], 3)} for x in merged],
        })
    return readings


def run(project):
    import ee
    ee.Initialize(project=project)
    zones_gj = geojson()
    zones = ee.FeatureCollection(zones_gj)
    aoi = zones.geometry().bounds().buffer(3000)
    s1 = (ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(aoi)
          .filter(ee.Filter.eq("instrumentMode", "IW"))
          .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
          .select("VV"))

    def smooth(img):
        return img.focalMedian(50, "circle", "meters").copyProperties(img, ["system:time_start"])

    before = s1.filterDate(*BEFORE).map(smooth).median()
    slope = ee.Terrain.slope(ee.Image("USGS/SRTMGL1_003"))
    permanent = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("seasonality").gte(10).unmask(0)

    def flood_mask(img):
        a = smooth(img)
        return (a.lt(WATER_DB).And(before.subtract(a).gt(DROP_DB)).And(before.gt(WATER_DB))
                .And(slope.lt(5)).And(permanent.Not()).rename("flood"))

    after = s1.filterDate(*AFTER)
    print("Sentinel-1 passes in event window:", after.size().getInfo())

    def per_image(img):
        millis = img.date().millis()
        return (flood_mask(img).reduceRegions(collection=zones, reducer=ee.Reducer.mean(), scale=30)
                .map(lambda f: f.set("millis", millis)))

    fc = after.map(per_image).flatten().select(["zoneId", "mean", "flood", "millis"], None, False).getInfo()
    observations = []
    for f in fc["features"]:
        p = f["properties"]
        frac = p.get("mean", p.get("flood"))
        observations.append({"zoneId": p["zoneId"], "t": p["millis"], "f": frac})
    zone_ids = [f["properties"]["zoneId"] for f in zones_gj["features"]]
    out = {"eventId": EVENT_ID, "source": "Sentinel-1 GRD via Google Earth Engine",
           "generated": datetime.now(timezone.utc).isoformat(),
           "readings": compute_readings(zone_ids, observations)}
    path = os.path.join(ROOT, "data", "flood_readings.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    for r in out["readings"]:
        print(f"{r['zoneId']}: {r['hoursFlooded']:>4} h flooded, peak {r['floodedFraction'] * 100:.0f}% of zone, "
              f"{len(r['observations'])} passes")
    print("wrote", path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="Google Cloud project registered for Earth Engine")
    run(ap.parse_args().project)

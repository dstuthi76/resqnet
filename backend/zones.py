"""Flood zones for the Hyderabad pilot.

Each zone is a ~2 km square around the approximate centre of a neighbourhood
that was badly hit in the October 2020 Hyderabad floods. Centroids are
approximate - adjust them (or replace this file with real ward boundaries
from GHMC open data) before any real use.

`names` holds the spellings people actually type, in English, Telugu, Tamil
and Hindi, so the rule-based extractor can geocode messages without GPS.
"""
import json
import math

HALF_SIDE_KM = 1.0

ZONES = [
    {"id": "HYD-MLK", "name": "Malakpet", "lat": 17.3730, "lng": 78.5000, "low_lying": True,
     "names": ["malakpet", "malak pet", "మలక్‌పేట్", "మలక్ పేట్", "மலக்பேட்", "मलकपेट"]},
    {"id": "HYD-CYG", "name": "Chandrayangutta", "lat": 17.3170, "lng": 78.4820, "low_lying": True,
     "names": ["chandrayangutta", "chandrayan gutta", "చాంద్రాయణగుట్ట", "சந்திராயங்குட்டா", "चंद्रायणगुट्टा"]},
    {"id": "HYD-FLK", "name": "Falaknuma", "lat": 17.3310, "lng": 78.4670, "low_lying": True,
     "names": ["falaknuma", "ఫలక్‌నుమా", "ఫలక్ నుమా", "பலக்னுமா", "फलकनुमा"]},
    {"id": "HYD-TLC", "name": "Tolichowki", "lat": 17.3980, "lng": 78.4130, "low_lying": True,
     "names": ["tolichowki", "toli chowki", "టోలిచౌకి", "டோலிசௌகி", "टोलीचौकी"]},
    {"id": "HYD-MDP", "name": "Mehdipatnam", "lat": 17.3950, "lng": 78.4400, "low_lying": False,
     "names": ["mehdipatnam", "మెహదీపట్నం", "மெஹதிபட்னம்", "मेहदीपट्टनम"]},
    {"id": "HYD-LBN", "name": "LB Nagar", "lat": 17.3470, "lng": 78.5520, "low_lying": True,
     "names": ["lb nagar", "l.b. nagar", "lbnagar", "ఎల్బీ నగర్", "எல்பி நகர்", "एलबी नगर"]},
    {"id": "HYD-SRN", "name": "Saroornagar", "lat": 17.3530, "lng": 78.5330, "low_lying": True,
     "names": ["saroornagar", "saroor nagar", "సరూర్‌నగర్", "సరూర్ నగర్", "சரூர்நகர்", "सरूरनगर"]},
    {"id": "HYD-UPL", "name": "Uppal", "lat": 17.4010, "lng": 78.5590, "low_lying": False,
     "names": ["uppal", "ఉప్పల్", "உப்பல்", "उप्पल"]},
    {"id": "HYD-HYT", "name": "Hayathnagar", "lat": 17.3270, "lng": 78.6040, "low_lying": False,
     "names": ["hayathnagar", "hayath nagar", "హయత్‌నగర్", "హయత్ నగర్", "ஹயத்நகர்", "हयातनगर"]},
    {"id": "HYD-MRB", "name": "Moosarambagh", "lat": 17.3720, "lng": 78.5130, "low_lying": True,
     "names": ["moosarambagh", "musarambagh", "మూసారాంబాగ్", "மூசாராம்பாக்", "मूसारामबाग"]},
]

ZONE_BY_ID = {z["id"]: z for z in ZONES}

# Approximate city-wide shelter list for the demo. Replace with the output of
# data/fetch_osm.py (real schools / community halls / hospitals from OSM).
SHELTERS = [
    {"name": f"{z['name']} relief camp (demo)", "type": "shelter",
     "lat": round(z["lat"] + 0.006, 5), "lng": round(z["lng"] - 0.004, 5)}
    for z in ZONES if not z["low_lying"]
] + [
    {"name": "Govt. school relief camp, Malakpet (demo)", "type": "shelter", "lat": 17.3800, "lng": 78.4950},
    {"name": "Community hall, Saroornagar (demo)", "type": "shelter", "lat": 17.3590, "lng": 78.5280},
]


def _deg_offsets(lat, km):
    dlat = km / 111.0
    dlng = km / (111.0 * math.cos(math.radians(lat)))
    return dlat, dlng


def zone_polygon(z):
    dlat, dlng = _deg_offsets(z["lat"], HALF_SIDE_KM)
    s, n = z["lat"] - dlat, z["lat"] + dlat
    w, e = z["lng"] - dlng, z["lng"] + dlng
    return [[w, s], [e, s], [e, n], [w, n], [w, s]]


def zone_for_point(lat, lng):
    """Return the zone containing the point, else the nearest zone within 3 km."""
    if lat is None or lng is None:
        return None
    best, best_d = None, 1e9
    for z in ZONES:
        dlat, dlng = _deg_offsets(z["lat"], HALF_SIDE_KM)
        if abs(lat - z["lat"]) <= dlat and abs(lng - z["lng"]) <= dlng:
            return z["id"]
        d = haversine_km(lat, lng, z["lat"], z["lng"])
        if d < best_d:
            best, best_d = z["id"], d
    return best if best_d <= 3.0 else None


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geojson():
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature",
             "properties": {"zoneId": z["id"], "name": z["name"], "lowLying": z["low_lying"]},
             "geometry": {"type": "Polygon", "coordinates": [zone_polygon(z)]}}
            for z in ZONES
        ],
    }


if __name__ == "__main__":
    print(json.dumps(geojson(), indent=1, ensure_ascii=False))

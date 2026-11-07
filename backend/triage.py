"""Deduplication + urgency ranking.

Every incoming SOS is either merged into an existing case (same emergency,
reported by someone else or relayed by another phone) or opens a new case.

Similarity combines:
  * distance between GPS points (strongest signal),
  * agreement of extracted facts (people count, vulnerable people, medical
    needs, water level) - this works across languages,
  * optional Gemini multilingual embedding similarity of the summaries.

Ranking is a transparent points formula so responders always see *why* a
case is on top, and can override it.
"""
import math
import time
import uuid

import db
from zones import ZONE_BY_ID, haversine_km, zone_for_point

GEO_RADIUS_KM = 0.15      # reports of one incident cluster within ~50 m; neighbours are 150 m+ apart
MERGE_THRESHOLD = 0.62
APPROX_RADIUS_KM = 1.5    # no-GPS reports are matched within their zone


def _jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 0.6
    return len(a & b) / len(a | b)


def attr_similarity(rec, case):
    s = []
    p1, p2 = rec.get("people"), case.get("people")
    if p1 and p2:
        s.append(1.0 if p1 == p2 else 0.4 if abs(p1 - p2) <= 1 else 0.0)
    else:
        s.append(0.5)
    s.append(_jaccard(rec.get("vulnerable", []), db.loads(case.get("vulnerable"))))
    s.append(_jaccard(rec.get("medical", []), db.loads(case.get("medical"))))
    w1, w2 = rec.get("water_level", "unknown"), case.get("water_level", "unknown")
    s.append(0.5 if "unknown" in (w1, w2) else 1.0 if w1 == w2 else 0.2)
    weights = [0.4, 0.2, 0.25, 0.15]
    return sum(a * b for a, b in zip(s, weights))


def cosine(a, b):
    if not a or not b:
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else None


def similarity(rec, case, case_emb=None):
    approx = rec.get("location_approx") or case.get("location_approx")
    d = haversine_km(rec["lat"], rec["lng"], case["lat"], case["lng"])
    if approx:
        if rec.get("zone_id") != case.get("zone_id") or d > APPROX_RADIUS_KM:
            return 0.0
        geo = 0.55
    else:
        if d > GEO_RADIUS_KM * 2:
            return 0.0
        geo = max(0.0, 1.0 - d / GEO_RADIUS_KM)
    attr = attr_similarity(rec, case)
    txt = cosine(rec.get("embedding"), case_emb)
    if txt is None:
        return 0.6 * geo + 0.4 * attr
    return 0.5 * geo + 0.3 * attr + 0.2 * txt


WATER_RANK = {"roof": 3, "neck": 4, "waist": 2, "ground": 1, "unknown": 0}


def score_case(c, now=None):
    """Return (score, reason). Higher = more urgent."""
    now = now or time.time()
    vul = set(db.loads(c.get("vulnerable")))
    med = set(db.loads(c.get("medical")))
    pts, why = 0.0, []
    critical = med & {"insulin", "dialysis", "injury", "breathing", "other_medical"}
    if critical:
        pts += 40
        why.append("medical: " + ", ".join(sorted(m.replace("_", " ") for m in critical)))
    elif "fever" in med:
        pts += 15
        why.append("fever")
    if "pregnant" in vul:
        pts += 25
        why.append("pregnant woman")
    if "infant" in vul:
        pts += 22
        why.append("infant")
    elif "child" in vul:
        pts += 15
        why.append("children")
    if "elderly" in vul:
        pts += 18
        why.append("elderly")
    if "disabled" in vul:
        pts += 18
        why.append("disabled person")
    wl = c.get("water_level") or "unknown"
    pts += {"neck": 30, "roof": 20, "waist": 12, "ground": 5}.get(wl, 0)
    if wl != "unknown":
        why.append({"neck": "neck-deep water", "roof": "on roof", "waist": "waist-deep water",
                    "ground": "ground floor flooded"}[wl])
    people = c.get("people") or 1
    pts += min(people, 12) * 2
    why.append(f"{people} people" if c.get("people") else "people count unknown")
    waited_h = max(0.0, (now - (c.get("first_seen") or now)) / 3600)
    pts += min(waited_h * 4, 30)
    if waited_h >= 0.5:
        why.append(f"waiting {waited_h:.1f} h")
    n = c.get("message_count") or 1
    if n > 1:
        pts += min(n, 6) * 1.5
        why.append(f"{n} reports")
    flags = db.loads(c.get("flags"))
    if "suspicious" in flags:
        pts *= 0.5
        why.append("⚠ flagged suspicious")
    return round(pts, 1), " · ".join(why)


def _case_embedding(case_id, emb_cache):
    return emb_cache.get(case_id)


def assign(rec, msg_ts_epoch, emb_cache):
    """Merge `rec` into the best matching case or create one. Returns case id.

    rec: extracted fields + lat/lng/location_approx/zone_id (+ optional embedding)
    """
    candidates = db.q(
        "SELECT * FROM cases WHERE status != 'closed' OR closed_at > ?", (time.time() - 3 * 3600,))
    best, best_s = None, 0.0
    for c in candidates:
        s = similarity(rec, c, _case_embedding(c["id"], emb_cache))
        if s > best_s:
            best, best_s = c, s
    now = time.time()
    if best and best_s >= MERGE_THRESHOLD:
        return _merge(best, rec, msg_ts_epoch, now, emb_cache)
    cid = "C-" + uuid.uuid4().hex[:6].upper()
    case = {
        "id": cid, "lat": rec["lat"], "lng": rec["lng"], "zone_id": rec.get("zone_id"),
        "location_approx": 1 if rec.get("location_approx") else 0,
        "people": rec.get("people"), "vulnerable": db.json_dumps(rec.get("vulnerable", [])),
        "medical": db.json_dumps(rec.get("medical", [])), "needs": db.json_dumps(rec.get("needs", [])),
        "water_level": rec.get("water_level", "unknown"), "summary": rec.get("summary_en", ""),
        "override_score": None, "status": "open", "team_id": None, "eta_min": None,
        "first_seen": msg_ts_epoch, "updated_at": now, "claimed_at": None, "closed_at": None,
        "message_count": 1, "flags": db.json_dumps(rec.get("flags", [])),
    }
    case["score"], case["reason"] = score_case(case, now)
    cols = ",".join(case)
    db.x(f"INSERT INTO cases ({cols}) VALUES ({','.join('?' * len(case))})", tuple(case.values()))
    if rec.get("embedding"):
        emb_cache[cid] = rec["embedding"]
    return cid


def _merge(c, rec, ts, now, emb_cache):
    n = (c["message_count"] or 1) + 1
    vul = sorted(set(db.loads(c["vulnerable"])) | set(rec.get("vulnerable", [])))
    med = sorted(set(db.loads(c["medical"])) | set(rec.get("medical", [])))
    needs = sorted(set(db.loads(c["needs"])) | set(rec.get("needs", [])))
    wl = max([c["water_level"] or "unknown", rec.get("water_level", "unknown")], key=lambda w: WATER_RANK[w])
    people = max(filter(None, [c["people"], rec.get("people")]), default=None)
    lat, lng, approx = c["lat"], c["lng"], c["location_approx"]
    if not rec.get("location_approx"):
        if approx:  # first precise fix replaces the zone-centroid guess
            lat, lng, approx = rec["lat"], rec["lng"], 0
        else:       # running mean of GPS fixes
            lat = c["lat"] + (rec["lat"] - c["lat"]) / n
            lng = c["lng"] + (rec["lng"] - c["lng"]) / n
    flags = sorted(set(db.loads(c["flags"])) | set(rec.get("flags", [])))
    upd = {
        "lat": lat, "lng": lng, "location_approx": approx, "people": people,
        "vulnerable": db.json_dumps(vul), "medical": db.json_dumps(med), "needs": db.json_dumps(needs),
        "water_level": wl, "message_count": n, "first_seen": min(c["first_seen"] or ts, ts),
        "updated_at": now, "flags": db.json_dumps(flags),
        "zone_id": c["zone_id"] or rec.get("zone_id"),
    }
    if len(rec.get("summary_en", "")) > len(c.get("summary") or "") and rec.get("people"):
        upd["summary"] = rec["summary_en"]
    merged = dict(c, **upd)
    upd["score"], upd["reason"] = score_case(merged, now)
    sets = ",".join(f"{k}=?" for k in upd)
    db.x(f"UPDATE cases SET {sets} WHERE id=?", (*upd.values(), c["id"]))
    if rec.get("embedding") and c["id"] not in emb_cache:
        emb_cache[c["id"]] = rec["embedding"]
    return c["id"]


def rescore_all():
    now = time.time()
    for c in db.q("SELECT * FROM cases WHERE status != 'closed'"):
        s, r = score_case(c, now)
        db.x("UPDATE cases SET score=?, reason=? WHERE id=?", (s, r, c["id"]))


def locate(fields, lat, lng):
    """Resolve a location for the record. Returns (lat, lng, approx, zone_id) or None."""
    if lat is not None and lng is not None:
        return lat, lng, False, zone_for_point(lat, lng) or fields.get("zone_id")
    zid = fields.get("zone_id")
    if zid and zid in ZONE_BY_ID:
        z = ZONE_BY_ID[zid]
        return z["lat"], z["lng"], True, zid
    return None


def suspicious_device(device_id, window_s=600, limit=5):
    """Fraud check: one device sending many SOS from far-apart places in a short time."""
    if not device_id:
        return False
    rows = db.q("SELECT lat, lng FROM messages WHERE device_id=? AND received_at > ? AND is_sos=1 AND lat IS NOT NULL",
                (device_id, time.time() - window_s))
    if len(rows) < limit:
        return False
    lats = [r["lat"] for r in rows]
    lngs = [r["lng"] for r in rows]
    return haversine_km(min(lats), min(lngs), max(lats), max(lngs)) > 2.0


def suggest_team(case):
    teams = db.q("SELECT * FROM teams")
    busy = {r["team_id"] for r in db.q("SELECT team_id FROM cases WHERE status='claimed'")}
    needs_med = bool(set(db.loads(case.get("medical"))) - {"fever"})
    best = None
    for t in teams:
        d = haversine_km(case["lat"], case["lng"], t["lat"], t["lng"])
        cost = d + (3 if t["id"] in busy else 0) + (2 if needs_med and not t["medical"] else 0)
        if best is None or cost < best[0]:
            best = (cost, t, d)
    if not best:
        return None
    _, t, d = best
    return {"team_id": t["id"], "team": t["name"], "distance_km": round(d, 1),
            "eta_min": int(10 + d / 8 * 60), "busy": t["id"] in busy,
            "why": f"nearest {'medical-capable ' if t['medical'] else ''}team, {d:.1f} km away"}

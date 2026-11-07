"""Intake pipeline: raw packets -> stored messages -> extraction -> cases."""
import logging
import time
from datetime import datetime

import db
import extract
import gemini
import config
import security
import triage

log = logging.getLogger("resqnet.pipeline")
_emb_cache = {}


def _epoch(ts, fallback):
    """Message timestamp as epoch; ignore timestamps far from now (old demo data,
    wrong phone clocks) so 'time waiting' stays meaningful."""
    if not ts:
        return fallback
    try:
        if isinstance(ts, (int, float)):
            t = float(ts) / (1000 if ts > 1e12 else 1)
        else:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return fallback
    return t if abs(t - fallback) < 48 * 3600 else fallback


def store_raw(packets, channel_default="api"):
    """Store new packets. Returns (new_ids, duplicate_ids, rejected_ids)."""
    new, dup, rejected = [], [], []
    now = time.time()
    for p in packets:
        pid = str(p.get("id") or "").strip()
        text = (p.get("text") or "").strip()
        if not pid or not text or len(text) > 2000:
            rejected.append(pid)
            continue
        if db.one("SELECT id FROM messages WHERE id=?", (pid,)):
            # same packet relayed by another phone: keep the lowest hop count
            dup.append(pid)
            continue
        sig = security.verify(p)
        if sig in ("invalid", "key_mismatch"):
            rejected.append(pid)
            log.warning("rejected %s: signature %s", pid, sig)
            continue
        lat, lng = p.get("lat"), p.get("lng")
        try:
            lat = float(lat) if lat not in (None, "") else None
            lng = float(lng) if lng not in (None, "") else None
        except (TypeError, ValueError):
            lat = lng = None
        truth = p.get("_incident")
        db.x("""INSERT INTO messages (id, received_at, channel, text, lat, lng, ts, hops, device_id,
                sig_status, is_sos, flags, truth) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'[]',?)""",
             (pid, now, p.get("channel") or channel_default, text, lat, lng, str(p.get("ts") or ""),
              int(p.get("hops") or 0), p.get("device_id"), sig, truth))
        new.append(pid)
    return new, dup, rejected


def process(ids):
    """Extract + triage the given stored messages. Returns list of touched case ids."""
    if not ids:
        return []
    rows = [db.one("SELECT * FROM messages WHERE id=?", (i,)) for i in ids]
    rows = [r for r in rows if r]
    results = extract.extract_many([r["text"] for r in rows], config.GEMINI_BATCH)

    embeddings = [None] * len(rows)
    if gemini.available():
        try:
            want = [k for k, (f, _) in enumerate(results) if f["is_sos"]]
            vecs = gemini.embed([results[k][0]["summary_en"] or rows[k]["text"] for k in want])
            for k, v in zip(want, vecs):
                embeddings[k] = v
        except gemini.GeminiError as e:
            log.warning("embeddings unavailable: %s", e)

    touched = []
    for k, (r, (fields, extractor)) in enumerate(zip(rows, results)):
        flags = []
        if r["sig_status"] == "unsigned" and r["channel"] == "mesh":
            flags.append("unsigned")
        if fields["is_sos"] and triage.suspicious_device(r["device_id"]):
            flags.append("suspicious")
        case_id = None
        if fields["is_sos"]:
            loc = triage.locate(fields, r["lat"], r["lng"])
            if loc:
                lat, lng, approx, zid = loc
                rec = dict(fields, lat=lat, lng=lng, location_approx=approx, zone_id=zid,
                           flags=flags, embedding=embeddings[k])
                if approx:
                    flags.append("location_approx")
                ts = _epoch(r["ts"], r["received_at"])
                case_id = triage.assign(rec, ts, _emb_cache)
                touched.append(case_id)
            else:
                flags.append("no_location")
        db.x("UPDATE messages SET extracted=?, extractor=?, is_sos=?, case_id=?, lang=?, flags=? WHERE id=?",
             (db.json_dumps(fields), extractor, 1 if fields["is_sos"] else 0, case_id,
              fields.get("language"), db.json_dumps(flags), r["id"]))
    return sorted(set(touched))


def reset_cache():
    _emb_cache.clear()

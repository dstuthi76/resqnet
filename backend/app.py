"""ResQNet backend + dashboard server.

Run locally:   cd backend && uvicorn app:app --reload --port 8000
Then open:     http://localhost:8000
"""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import queue
import threading
import time

import requests
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import alerts
import chain
import config
import db
import gemini
import pipeline
import triage
import zones as Z

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("resqnet")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# ------------------------------------------------------------ realtime (SSE)
_subscribers = set()
_loop = None


def broadcast(kind, payload=None):
    if _loop is None:
        return
    msg = json.dumps({"kind": kind, "payload": payload or {}, "ts": time.time()})
    for q in list(_subscribers):
        _loop.call_soon_threadsafe(q.put_nowait, msg)


async def events(request):
    q = asyncio.Queue()
    _subscribers.add(q)

    async def gen():
        try:
            yield {"event": "hello", "data": "{}"}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield {"event": "update", "data": msg}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            _subscribers.discard(q)

    return EventSourceResponse(gen())


# ------------------------------------------------------ background worker
_work = queue.Queue()
_state = {"queued": 0, "processing": False}


def _worker():
    while True:
        ids = _work.get()
        _state["processing"] = True
        try:
            touched = pipeline.process(ids)
            broadcast("cases", {"touched": touched, "new_messages": len(ids)})
        except Exception:
            log.exception("processing failed")
        finally:
            _state["queued"] = max(0, _state["queued"] - len(ids))
            _state["processing"] = not _work.empty()


def enqueue(ids, chunk=50):
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        _state["queued"] += len(part)
        _work.put(part)


def _rescore_loop():
    while True:
        time.sleep(60)
        try:
            triage.rescore_all()
            broadcast("rescore")
        except Exception:
            log.exception("rescore failed")


# ------------------------------------------------------------------ helpers
async def body(request):
    try:
        return await request.json()
    except Exception:
        return {}


def err(msg, code=400):
    return JSONResponse({"error": msg}, status_code=code)


def admin_ok(request):
    return not config.ADMIN_TOKEN or request.query_params.get("token") == config.ADMIN_TOKEN


def case_out(c):
    team = db.one("SELECT name FROM teams WHERE id=?", (c["team_id"],)) if c.get("team_id") else None
    return {
        **{k: c[k] for k in ["id", "lat", "lng", "zone_id", "people", "water_level", "summary", "reason",
                             "status", "team_id", "eta_min", "message_count", "first_seen", "claimed_at", "closed_at"]},
        "location_approx": bool(c["location_approx"]),
        "vulnerable": db.loads(c["vulnerable"]), "medical": db.loads(c["medical"]),
        "needs": db.loads(c["needs"]), "flags": db.loads(c["flags"]),
        "score": c["override_score"] if c["override_score"] is not None else c["score"],
        "ai_score": c["score"], "overridden": c["override_score"] is not None,
        "team": team["name"] if team else None,
        "zone": Z.ZONE_BY_ID.get(c["zone_id"], {}).get("name"),
    }


def seed():
    if not db.one("SELECT id FROM teams LIMIT 1"):
        teams = [
            ("T1", "NDRF Boat Team 1", "ndrf", 17.3650, 78.4900, 2, 1),
            ("T2", "SDRF Boat Team 2", "sdrf", 17.3300, 78.4750, 2, 0),
            ("T3", "Fire & Rescue Unit 3", "fire", 17.3500, 78.5450, 1, 1),
            ("T4", "Volunteer Boat Group 4", "volunteer", 17.3950, 78.4300, 1, 0),
            ("T5", "Police Rescue Team 5", "police", 17.3700, 78.5200, 1, 0),
            ("T6", "Relief NGO A - Medical Boat", "ngo", 17.3200, 78.4900, 1, 1),
        ]
        for t in teams:
            db.x("INSERT INTO teams VALUES (?,?,?,?,?,?,?)", t)
    chain.ensure_ngos()


# ---------------------------------------------------------------- SOS API
async def post_sos(request):
    data = await body(request)
    packets = data if isinstance(data, list) else data.get("packets") if isinstance(data, dict) and "packets" in data else [data]
    packets = [p for p in packets if isinstance(p, dict)][:1000]
    new, dup, rejected = pipeline.store_raw(packets, channel_default=request.query_params.get("channel", "api"))
    enqueue(new)
    broadcast("messages", {"new": len(new)})
    # every stored id (new or already known) is acknowledged so phones can drop it from the mesh
    return JSONResponse({"accepted": new, "duplicates": dup, "rejected": rejected, "acks": list(dict.fromkeys(new + dup))})


async def sos_status(request):
    ids = [i for i in request.query_params.get("ids", "").split(",") if i][:200]
    out = {}
    for i in ids:
        m = db.one("SELECT case_id, is_sos FROM messages WHERE id=?", (i,))
        if not m:
            continue
        if not m["case_id"]:
            out[i] = {"status": "received" if m["is_sos"] is None else "reviewing"}
            continue
        c = db.one("SELECT * FROM cases WHERE id=?", (m["case_id"],))
        team = db.one("SELECT name FROM teams WHERE id=?", (c["team_id"],)) if c["team_id"] else None
        out[i] = {"status": c["status"], "case_id": c["id"], "team": team["name"] if team else None,
                  "eta_min": c["eta_min"]}
    return JSONResponse(out)


async def list_cases(request):
    st = request.query_params.get("status")
    rows = db.q("SELECT * FROM cases" + (" WHERE status=?" if st else ""), (st,) if st else ())
    out = [case_out(c) for c in rows]
    out.sort(key=lambda c: (c["status"] == "closed", c["status"] == "claimed", -(c["score"] or 0)))
    return JSONResponse(out)


async def get_case(request):
    c = db.one("SELECT * FROM cases WHERE id=?", (request.path_params["cid"],))
    if not c:
        return err("not found", 404)
    msgs = db.q("SELECT id, channel, text, lang, lat, lng, ts, hops, sig_status, extractor, flags FROM messages "
                "WHERE case_id=? ORDER BY received_at", (c["id"],))
    for m in msgs:
        m["flags"] = db.loads(m["flags"])
    return JSONResponse({**case_out(c), "messages": msgs, "suggestion": triage.suggest_team(c)})


async def claim_case(request):
    cid = request.path_params["cid"]
    d = await body(request)
    team = db.one("SELECT * FROM teams WHERE id=?", (d.get("team_id"),))
    if not team:
        return err("unknown team")
    c = db.one("SELECT * FROM cases WHERE id=?", (cid,))
    if not c:
        return err("not found", 404)
    eta = d.get("eta_min")
    if eta is None:
        eta = int(10 + Z.haversine_km(c["lat"], c["lng"], team["lat"], team["lng"]) / 8 * 60)
    # atomic: only an open case can be claimed -> no two boats to one house
    cur = db.x("UPDATE cases SET status='claimed', team_id=?, eta_min=?, claimed_at=? WHERE id=? AND status='open'",
               (team["id"], int(eta), time.time(), cid))
    if cur.rowcount == 0:
        cur_c = db.one("SELECT status, team_id FROM cases WHERE id=?", (cid,))
        return err(f"case already {cur_c['status']} by {cur_c['team_id']}", 409)
    broadcast("cases", {"touched": [cid], "action": "claimed"})
    return JSONResponse(case_out(db.one("SELECT * FROM cases WHERE id=?", (cid,))))


async def close_case(request):
    cid = request.path_params["cid"]
    cur = db.x("UPDATE cases SET status='closed', closed_at=? WHERE id=? AND status!='closed'", (time.time(), cid))
    if cur.rowcount == 0:
        return err("not found or already closed", 409)
    c = db.one("SELECT team_id FROM cases WHERE id=?", (cid,))
    if c["team_id"] == "T6":
        db.x("UPDATE ngos SET cases_reached = cases_reached + 1 WHERE id='ngo1'")
    broadcast("cases", {"touched": [cid], "action": "closed"})
    return JSONResponse({"ok": True})


async def release_case(request):
    cid = request.path_params["cid"]
    db.x("UPDATE cases SET status='open', team_id=NULL, eta_min=NULL, claimed_at=NULL WHERE id=? AND status='claimed'", (cid,))
    broadcast("cases", {"touched": [cid], "action": "released"})
    return JSONResponse({"ok": True})


async def override_case(request):
    cid = request.path_params["cid"]
    d = await body(request)
    score = d.get("score")
    db.x("UPDATE cases SET override_score=? WHERE id=?", (None if score in (None, "") else float(score), cid))
    broadcast("cases", {"touched": [cid], "action": "override"})
    return JSONResponse({"ok": True})


async def list_teams(request):
    teams = db.q("SELECT * FROM teams")
    busy = {r["team_id"]: r["n"] for r in db.q("SELECT team_id, COUNT(*) n FROM cases WHERE status='claimed' GROUP BY team_id")}
    for t in teams:
        t["active_cases"] = busy.get(t["id"], 0)
    return JSONResponse(teams)


async def unlocated(request):
    rows = db.q("SELECT id, text, channel, ts FROM messages WHERE is_sos=1 AND case_id IS NULL ORDER BY received_at DESC LIMIT 50")
    return JSONResponse(rows)


async def stats(request):
    s = db.one("""SELECT COUNT(*) total, SUM(CASE WHEN is_sos=1 THEN 1 ELSE 0 END) sos,
                  SUM(CASE WHEN is_sos=0 THEN 1 ELSE 0 END) noise,
                  SUM(CASE WHEN extractor='gemini' THEN 1 ELSE 0 END) by_gemini FROM messages""")
    c = db.one("""SELECT COUNT(*) cases, SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open,
                  SUM(CASE WHEN status='claimed' THEN 1 ELSE 0 END) claimed,
                  SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) closed FROM cases""")
    langs = db.q("SELECT lang, COUNT(*) n FROM messages WHERE lang IS NOT NULL GROUP BY lang")
    return JSONResponse({**{k: (v or 0) for k, v in s.items()}, **{k: (v or 0) for k, v in c.items()},
                         "languages": {r["lang"]: r["n"] for r in langs},
                         "queued": _state["queued"], "processing": _state["processing"],
                         "ai": "gemini" if gemini.available() else "rules (offline)",
                         "chain": chain.mode()})


async def shelters(request):
    path = os.path.join(config.DATA_DIR, "shelters.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return JSONResponse(json.load(f))
    return JSONResponse(Z.SHELTERS)


async def zones_geo(request):
    return JSONResponse(Z.geojson())


# ------------------------------------------------------------------ demo
async def demo_load(request):
    if not admin_ok(request):
        return err("forbidden", 403)
    limit = int(request.query_params.get("limit", "10000"))
    with open(os.path.join(config.DATA_DIR, "sos_messages.json"), encoding="utf-8") as f:
        recs = json.load(f)[:limit]
    new, dup, rej = pipeline.store_raw(recs, channel_default="demo")
    enqueue(new)
    broadcast("messages", {"new": len(new)})
    return JSONResponse({"loaded": len(new), "duplicates": len(dup), "rejected": len(rej)})


async def demo_reset(request):
    if not admin_ok(request):
        return err("forbidden", 403)
    db.reset_all()
    pipeline.reset_cache()
    seed()
    broadcast("reset")
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- relief
async def enroll(request):
    d = await body(request)
    name, phone, zone_id = (d.get("name") or "").strip(), (d.get("phone") or "").strip(), d.get("zone_id")
    if not name or not phone or zone_id not in Z.ZONE_BY_ID:
        return err("name, phone and a valid zone_id are required")
    idh = chain.id_hash(name, phone)
    if db.one("SELECT 1 FROM households WHERE id_hash=?", (idh,)):
        return err("already enrolled", 409)
    wallet, key = chain.new_wallet()
    try:
        tx = await asyncio.to_thread(chain.register_household, idh, zone_id, wallet)
    except Exception as e:
        return err(f"chain error: {e}", 502)
    db.x("INSERT INTO households VALUES (?,?,?,?,?,?,?)",
         (idh, zone_id, wallet, key, d.get("channel") or "app", time.time(), tx))
    broadcast("relief")
    return JSONResponse({"id_hash": idh, "zone_id": zone_id, "wallet": wallet, "tx": tx,
                         "note": "Only the hash and zone are stored on-chain. No name or phone."})


async def demo_enroll(request):
    per_zone = int(request.query_params.get("per_zone", "4"))
    n = 0
    for z in Z.ZONES:
        for i in range(per_zone):
            idh = chain.id_hash(f"demo-{z['id']}-{i}", "0000000000")
            if db.one("SELECT 1 FROM households WHERE id_hash=?", (idh,)):
                continue
            wallet, key = chain.new_wallet()
            try:
                tx = await asyncio.to_thread(chain.register_household, idh, z["id"], wallet)
            except Exception as e:
                return err(f"chain error after {n} households: {e}", 502)
            db.x("INSERT INTO households VALUES (?,?,?,?,?,?,?)", (idh, z["id"], wallet, key, "ngo-field", time.time(), tx))
            n += 1
    if chain.mode() == "sim" and chain.pool_balance_wei() == 0:
        chain.fund_pool(10**18)  # 1 test ETH in the simulator
    broadcast("relief")
    return JSONResponse({"enrolled": n})


async def relief_state(request):
    event_id = request.query_params.get("event", config.DEFAULT_EVENT_ID)
    hh = {r["zone_id"]: r["n"] for r in db.q("SELECT zone_id, COUNT(*) n FROM households GROUP BY zone_id")}
    readings = {r["zone_id"]: r for r in db.q("SELECT * FROM flood_readings WHERE event_id=?", (event_id,))}
    confs = {}
    for r in db.q("SELECT * FROM confirmations WHERE event_id=?", (event_id,)):
        confs.setdefault(r["zone_id"], []).append(r)
    trig = {r["zone_id"]: r for r in db.q("SELECT * FROM triggered WHERE event_id=?", (event_id,))}
    paid = {r["zone_id"]: r for r in db.q("SELECT zone_id, COUNT(*) n, SUM(CAST(amount_wei AS REAL)) total FROM payouts WHERE event_id=? GROUP BY zone_id", (event_id,))}
    zones_out = []
    for z in Z.ZONES:
        r = readings.get(z["id"])
        zones_out.append({
            "zoneId": z["id"], "name": z["name"], "households": hh.get(z["id"], 0),
            "hours": r["hours"] if r else None, "fraction": r["fraction"] if r else None,
            "reading_tx": r["tx"] if r else None, "reading_source": r["source"] if r else None,
            "confirmations": [{"ngo_id": c["ngo_id"], "tx": c["tx"]} for c in confs.get(z["id"], [])],
            "triggered": z["id"] in trig, "trigger_tx": trig[z["id"]]["tx"] if z["id"] in trig else None,
            "paid_count": paid[z["id"]]["n"] if z["id"] in paid else 0,
            "eligible": db.one("SELECT COUNT(*) n FROM households WHERE zone_id=? AND enrolled_at <= ?",
                               (z["id"], trig[z["id"]]["ts"] - config.WAITING_PERIOD_S))["n"] if z["id"] in trig else None,
            "paid_wei": int(paid[z["id"]]["total"] or 0) if z["id"] in paid else 0,
        })
    payouts = db.q("SELECT * FROM payouts WHERE event_id=? ORDER BY ts DESC LIMIT 100", (event_id,))
    try:
        pool = chain.pool_balance_wei()
    except Exception as e:
        pool = None
        log.warning("pool balance unavailable: %s", e)
    return JSONResponse({
        "event_id": event_id, "mode": chain.mode(), "zones": zones_out, "payouts": payouts,
        "ngos": db.q("SELECT id, name, wallet, cases_reached, deliveries FROM ngos"),
        "pool_wei": pool, "payout_wei": config.PAYOUT_WEI,
        "rule": {"hours": config.HOURS_THRESHOLD, "confirmations": config.CONFIRMATIONS_REQUIRED},
        "explorer_tx": config.EXPLORER_TX_URL if chain.mode() == "sepolia" else None,
        "explorer_addr": config.EXPLORER_ADDR_URL if chain.mode() == "sepolia" else None,
        "contract": config.CONTRACT_ADDRESS if chain.mode() == "sepolia" else None,
        "relief_log": db.q("SELECT * FROM relief_log ORDER BY ts DESC LIMIT 50"),
    })


async def load_readings(request):
    """Load Earth Engine output (data/flood_readings.json) or the labelled sample, and submit on-chain."""
    real = os.path.join(config.DATA_DIR, "flood_readings.json")
    sample = os.path.join(config.DATA_DIR, "flood_readings_sample.json")
    path = real if os.path.exists(real) else sample
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    event_id = data.get("eventId", config.DEFAULT_EVENT_ID)
    src = data.get("source", "earth-engine")
    done = []
    for r in data["readings"]:
        if r["zoneId"] not in Z.ZONE_BY_ID:
            continue
        try:
            tx = await asyncio.to_thread(chain.submit_reading, r["zoneId"], event_id, int(r["hoursFlooded"]),
                                         r.get("floodedFraction"), src)
        except Exception as e:
            return err(f"chain error at {r['zoneId']}: {e}", 502)
        done.append({"zoneId": r["zoneId"], "tx": tx})
    broadcast("relief")
    return JSONResponse({"file": os.path.basename(path), "source": src, "event_id": event_id, "submitted": done})


async def confirm(request):
    d = await body(request)
    try:
        tx = await asyncio.to_thread(chain.confirm_flood, d.get("ngo_id"), d.get("zone_id"),
                                     d.get("event_id") or config.DEFAULT_EVENT_ID)
    except ValueError as e:
        return err(str(e), 409)
    except Exception as e:
        return err(f"chain error: {e}", 502)
    broadcast("relief")
    return JSONResponse({"tx": tx})


async def process_payouts(request):
    d = await body(request)
    try:
        tx = await asyncio.to_thread(chain.process_payouts, d.get("zone_id"), d.get("event_id") or config.DEFAULT_EVENT_ID)
    except Exception as e:
        return err(f"chain error: {e}", 502)
    broadcast("relief")
    return JSONResponse({"tx": tx})


async def fund(request):
    d = await body(request)
    try:
        tx = chain.fund_pool(int(float(d.get("eth", 0.1)) * 10**18))
    except Exception as e:
        return err(str(e))
    broadcast("relief")
    return JSONResponse({"tx": tx})


async def relief_log(request):
    form = await request.form()
    ngo_id = form.get("ngo_id") or "ngo1"
    batch, frm, to = form.get("batch_id"), form.get("from_party"), form.get("to_party")
    photo = form.get("photo")
    if not (batch and frm and to and photo is not None and hasattr(photo, "read")):
        return err("batch_id, from_party, to_party and photo are required")
    content = await photo.read()
    photo_hash = hashlib.sha256(content).hexdigest()
    cid = None
    if config.PINATA_JWT:
        try:
            r = requests.post("https://api.pinata.cloud/pinning/pinFileToIPFS",
                              headers={"Authorization": f"Bearer {config.PINATA_JWT}"},
                              files={"file": (photo.filename or "proof.jpg", content)}, timeout=60)
            r.raise_for_status()
            cid = r.json().get("IpfsHash")
        except Exception as e:
            log.warning("Pinata upload failed: %s", e)
    try:
        tx = await asyncio.to_thread(chain.log_relief, ngo_id, batch, frm, to, photo_hash)
    except Exception as e:
        return err(f"chain error: {e}", 502)
    db.x("INSERT INTO relief_log VALUES (?,?,?,?,?,?,?)", (batch, frm, to, photo_hash, cid, tx, time.time()))
    db.x("UPDATE ngos SET deliveries = deliveries + 1 WHERE id=?", (ngo_id,))
    broadcast("relief")
    return JSONResponse({"photo_hash": photo_hash, "ipfs_cid": cid, "tx": tx})


# ---------------------------------------------------------------- alerts
async def alert_forecast(request):
    return JSONResponse(await asyncio.to_thread(alerts.forecast))


async def alert_generate(request):
    d = await body(request)
    shelter = d.get("shelter") or (Z.SHELTERS[0]["name"] if Z.SHELTERS else "the nearest relief camp")
    return JSONResponse(await asyncio.to_thread(alerts.generate, d.get("lang", "te"), d.get("zone_ids") or [], shelter))


async def alert_audio(request):
    d = await body(request)
    mp3 = await asyncio.to_thread(alerts.tts_mp3, d.get("text", ""), d.get("lang", "te"))
    if not mp3:
        return err("server TTS unavailable - use browser speech", 503)
    return Response(mp3, media_type="audio/mpeg")


# -------------------------------------------------------------------- misc
async def health(request):
    return JSONResponse({"ok": True, "ai": "gemini" if gemini.available() else "rules", "chain": chain.mode()})


def page(name):
    async def handler(request):
        return FileResponse(os.path.join(STATIC, name))
    return handler


@contextlib.asynccontextmanager
async def lifespan(app):
    global _loop
    _loop = asyncio.get_running_loop()
    db.conn()
    seed()
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_rescore_loop, daemon=True).start()
    log.info("ResQNet ready - AI: %s, chain: %s", "gemini" if gemini.available() else "rules", chain.mode())
    yield


routes = [
    Route("/", page("index.html")),
    Route("/relief", page("relief.html")),
    Route("/alerts", page("alerts.html")),
    Route("/mesh", page("mesh.html")),
    Route("/enroll", page("enroll.html")),
    Route("/api/health", health),
    Route("/api/events", events),
    Route("/api/sos", post_sos, methods=["POST"]),
    Route("/api/sos/status", sos_status),
    Route("/api/cases", list_cases),
    Route("/api/cases/{cid}", get_case),
    Route("/api/cases/{cid}/claim", claim_case, methods=["POST"]),
    Route("/api/cases/{cid}/close", close_case, methods=["POST"]),
    Route("/api/cases/{cid}/release", release_case, methods=["POST"]),
    Route("/api/cases/{cid}/override", override_case, methods=["POST"]),
    Route("/api/teams", list_teams),
    Route("/api/unlocated", unlocated),
    Route("/api/stats", stats),
    Route("/api/shelters", shelters),
    Route("/api/zones", zones_geo),
    Route("/api/demo/load", demo_load, methods=["POST"]),
    Route("/api/demo/reset", demo_reset, methods=["POST"]),
    Route("/api/demo/enroll", demo_enroll, methods=["POST"]),
    Route("/api/households", enroll, methods=["POST"]),
    Route("/api/relief/state", relief_state),
    Route("/api/relief/readings/load", load_readings, methods=["POST"]),
    Route("/api/relief/confirm", confirm, methods=["POST"]),
    Route("/api/relief/process", process_payouts, methods=["POST"]),
    Route("/api/relief/fund", fund, methods=["POST"]),
    Route("/api/relief/log", relief_log, methods=["POST"]),
    Route("/api/alerts/forecast", alert_forecast),
    Route("/api/alerts/generate", alert_generate, methods=["POST"]),
    Route("/api/alerts/audio", alert_audio, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan,
                middleware=[Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])])

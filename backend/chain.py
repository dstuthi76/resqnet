"""Bridge between the backend and the ResQNetRelief contract.

CHAIN_MODE=sim      built-in simulator that applies exactly the same rules as
                    the Solidity contract, stored in SQLite. No keys needed.
CHAIN_MODE=sepolia  real transactions on the Sepolia testnet via web3.py,
                    signed by the oracle wallet and the NGO wallets.
"""
import hashlib
import json
import logging
import os
import secrets
import threading
import time

import config
import db

log = logging.getLogger("resqnet.chain")
_tx_lock = threading.Lock()

NGO_NAMES = [n.strip() for n in config.env("NGO_NAMES", "Relief NGO A,Relief NGO B,Relief NGO C").split(",")]


def b32(s):
    raw = s.encode("utf-8")
    if len(raw) > 32:
        raise ValueError(f"'{s}' longer than 32 bytes")
    return raw.ljust(32, b"\0")


def id_hash(*parts):
    h = hashlib.sha256((config.ID_SALT + "|" + "|".join(p.strip().lower() for p in parts)).encode()).hexdigest()
    return h


def _fake_tx(*parts):
    return "0x" + hashlib.sha256(("|".join(map(str, parts)) + str(time.time_ns())).encode()).hexdigest()


def mode():
    return "sepolia" if config.CHAIN_MODE == "sepolia" else "sim"


# =========================================================== Sepolia (web3)
_w3 = None
_contract = None

FALLBACK_ABI = json.loads("""[
 {"type":"function","name":"registerHousehold","stateMutability":"nonpayable","inputs":[{"name":"idHash","type":"bytes32"},{"name":"zoneId","type":"bytes32"},{"name":"wallet","type":"address"}],"outputs":[]},
 {"type":"function","name":"submitFloodReading","stateMutability":"nonpayable","inputs":[{"name":"zoneId","type":"bytes32"},{"name":"eventId","type":"bytes32"},{"name":"hoursFlooded","type":"uint32"}],"outputs":[]},
 {"type":"function","name":"confirmFlood","stateMutability":"nonpayable","inputs":[{"name":"zoneId","type":"bytes32"},{"name":"eventId","type":"bytes32"}],"outputs":[]},
 {"type":"function","name":"processPayouts","stateMutability":"nonpayable","inputs":[{"name":"zoneId","type":"bytes32"},{"name":"eventId","type":"bytes32"}],"outputs":[]},
 {"type":"function","name":"logRelief","stateMutability":"nonpayable","inputs":[{"name":"batchId","type":"bytes32"},{"name":"fromParty","type":"string"},{"name":"toParty","type":"string"},{"name":"photoHash","type":"bytes32"}],"outputs":[]},
 {"type":"function","name":"fundPool","stateMutability":"payable","inputs":[],"outputs":[]},
 {"type":"function","name":"poolBalance","stateMutability":"view","inputs":[],"outputs":[{"name":"","type":"uint256"}]},
 {"type":"event","name":"PayoutTriggered","anonymous":false,"inputs":[{"name":"zoneId","type":"bytes32","indexed":true},{"name":"eventId","type":"bytes32","indexed":true}]},
 {"type":"event","name":"PayoutSent","anonymous":false,"inputs":[{"name":"idHash","type":"bytes32","indexed":true},{"name":"zoneId","type":"bytes32","indexed":true},{"name":"eventId","type":"bytes32","indexed":true},{"name":"wallet","type":"address","indexed":false},{"name":"amount","type":"uint256","indexed":false}]},
 {"type":"event","name":"PayoutPaused","anonymous":false,"inputs":[{"name":"zoneId","type":"bytes32","indexed":true},{"name":"eventId","type":"bytes32","indexed":true},{"name":"cursor","type":"uint256","indexed":false},{"name":"reason","type":"string","indexed":false}]}
]""")


def _abi():
    art = os.path.join(config.ROOT, "contracts", "artifacts", "contracts", "ResQNetRelief.sol", "ResQNetRelief.json")
    if os.path.exists(art):
        with open(art) as f:
            return json.load(f)["abi"]
    return FALLBACK_ABI


def _web3():
    global _w3, _contract
    if _w3 is None:
        try:
            from web3 import Web3
        except ImportError as e:
            raise RuntimeError("CHAIN_MODE=sepolia needs `pip install web3`") from e
        if not (config.SEPOLIA_RPC_URL and config.ORACLE_PRIVATE_KEY and config.CONTRACT_ADDRESS):
            raise RuntimeError("Set SEPOLIA_RPC_URL, ORACLE_PRIVATE_KEY and CONTRACT_ADDRESS in .env")
        _w3 = Web3(Web3.HTTPProvider(config.SEPOLIA_RPC_URL, request_kwargs={"timeout": 60}))
        _contract = _w3.eth.contract(address=Web3.to_checksum_address(config.CONTRACT_ADDRESS), abi=_abi())
    return _w3, _contract


def _send(fn, private_key, value=0):
    w3, _ = _web3()
    acct = w3.eth.account.from_key(private_key)
    with _tx_lock:
        tx = fn.build_transaction({
            "from": acct.address, "value": value, "chainId": w3.eth.chain_id,
            "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        })
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        h = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"transaction reverted: {h.hex()}")
    return receipt


def _hex(h):
    s = h.hex() if hasattr(h, "hex") else str(h)
    return s if s.startswith("0x") else "0x" + s


def _record_payouts_from_receipt(receipt, zone_id, event_id):
    _, c = _web3()
    txh = _hex(receipt.transactionHash)
    if c.events.PayoutTriggered().process_receipt(receipt):
        db.x("INSERT OR IGNORE INTO triggered VALUES (?,?,?,?)", (zone_id, event_id, txh, time.time()))
    for ev in c.events.PayoutSent().process_receipt(receipt):
        idh = _hex(ev["args"]["idHash"])[2:]
        db.x("INSERT OR IGNORE INTO payouts VALUES (?,?,?,?,?,?,?)",
             (idh, zone_id, event_id, ev["args"]["wallet"], str(ev["args"]["amount"]), txh, time.time()))


# ============================================================== public API

def ensure_ngos():
    if db.one("SELECT id FROM ngos LIMIT 1"):
        return
    if mode() == "sepolia" and config.NGO_PRIVATE_KEYS:
        w3, _ = _web3()
        wallets = [w3.eth.account.from_key(k).address for k in config.NGO_PRIVATE_KEYS]
    else:
        wallets = ["0x" + secrets.token_hex(20) for _ in NGO_NAMES]
    for i, w in enumerate(wallets):
        name = NGO_NAMES[i] if i < len(NGO_NAMES) else f"NGO {i + 1}"
        db.x("INSERT INTO ngos (id, name, wallet) VALUES (?,?,?)", (f"ngo{i + 1}", name, w))


def new_wallet():
    """Custodial wallet for a household. Households never see it."""
    if mode() == "sepolia":
        from eth_account import Account
        a = Account.create()
        return a.address, a.key.hex()
    return "0x" + secrets.token_hex(20), None


def register_household(idh, zone_id, wallet):
    if mode() == "sepolia":
        _, c = _web3()
        r = _send(c.functions.registerHousehold(bytes.fromhex(idh), b32(zone_id), wallet), config.ORACLE_PRIVATE_KEY)
        return _hex(r.transactionHash)
    return _fake_tx("register", idh)


def pool_balance_wei():
    if mode() == "sepolia":
        _, c = _web3()
        return int(c.functions.poolBalance().call())
    return int(db.kv_get("sim_pool_wei", 0))


def fund_pool(amount_wei):
    if mode() == "sepolia":
        raise RuntimeError("On Sepolia, fund the pool from MetaMask or with the deploy script (POOL_FUND_ETH).")
    db.kv_set("sim_pool_wei", pool_balance_wei() + int(amount_wei))
    return _fake_tx("fund", amount_wei)


def submit_reading(zone_id, event_id, hours, fraction=None, source="earth-engine"):
    if mode() == "sepolia":
        _, c = _web3()
        r = _send(c.functions.submitFloodReading(b32(zone_id), b32(event_id), int(hours)), config.ORACLE_PRIVATE_KEY)
        txh = _hex(r.transactionHash)
        db.x("INSERT OR REPLACE INTO flood_readings VALUES (?,?,?,?,?,?,?)",
             (zone_id, event_id, int(hours), fraction, source, txh, time.time()))
        _record_payouts_from_receipt(r, zone_id, event_id)
        return txh
    txh = _fake_tx("reading", zone_id, event_id, hours)
    db.x("INSERT OR REPLACE INTO flood_readings VALUES (?,?,?,?,?,?,?)",
         (zone_id, event_id, int(hours), fraction, source, txh, time.time()))
    _sim_try_payout(zone_id, event_id, txh)
    return txh


def confirm_flood(ngo_id, zone_id, event_id):
    ngo = db.one("SELECT * FROM ngos WHERE id=?", (ngo_id,))
    if not ngo:
        raise ValueError("unknown NGO")
    if db.one("SELECT 1 FROM confirmations WHERE zone_id=? AND event_id=? AND ngo_id=?", (zone_id, event_id, ngo_id)):
        raise ValueError("this NGO already confirmed this zone for this event")
    if mode() == "sepolia":
        idx = int(ngo_id.replace("ngo", "")) - 1
        if idx >= len(config.NGO_PRIVATE_KEYS):
            raise RuntimeError("no private key configured for this NGO (NGO_PRIVATE_KEYS)")
        _, c = _web3()
        r = _send(c.functions.confirmFlood(b32(zone_id), b32(event_id)), config.NGO_PRIVATE_KEYS[idx])
        txh = _hex(r.transactionHash)
        db.x("INSERT INTO confirmations VALUES (?,?,?,?,?)", (zone_id, event_id, ngo_id, txh, time.time()))
        _record_payouts_from_receipt(r, zone_id, event_id)
        return txh
    txh = _fake_tx("confirm", ngo_id, zone_id, event_id)
    db.x("INSERT INTO confirmations VALUES (?,?,?,?,?)", (zone_id, event_id, ngo_id, txh, time.time()))
    _sim_try_payout(zone_id, event_id, txh)
    return txh


def process_payouts(zone_id, event_id):
    if mode() == "sepolia":
        _, c = _web3()
        r = _send(c.functions.processPayouts(b32(zone_id), b32(event_id)), config.ORACLE_PRIVATE_KEY)
        _record_payouts_from_receipt(r, zone_id, event_id)
        return _hex(r.transactionHash)
    txh = _fake_tx("process", zone_id, event_id)
    if db.one("SELECT 1 FROM triggered WHERE zone_id=? AND event_id=?", (zone_id, event_id)):
        _sim_pay(zone_id, event_id, txh)
    return txh


def log_relief(ngo_id, batch_id, from_party, to_party, photo_hash_hex):
    if mode() == "sepolia":
        idx = int(ngo_id.replace("ngo", "")) - 1
        _, c = _web3()
        r = _send(c.functions.logRelief(b32(batch_id[:32]), from_party, to_party, bytes.fromhex(photo_hash_hex)),
                  config.NGO_PRIVATE_KEYS[idx])
        return _hex(r.transactionHash)
    return _fake_tx("relief", batch_id)


# ---------------------------------------------------- simulator (same rules)

def _sim_try_payout(zone_id, event_id, txh):
    if db.one("SELECT 1 FROM triggered WHERE zone_id=? AND event_id=?", (zone_id, event_id)):
        return
    r = db.one("SELECT hours FROM flood_readings WHERE zone_id=? AND event_id=?", (zone_id, event_id))
    n = db.one("SELECT COUNT(*) AS n FROM confirmations WHERE zone_id=? AND event_id=?", (zone_id, event_id))["n"]
    if not r or r["hours"] < config.HOURS_THRESHOLD or n < config.CONFIRMATIONS_REQUIRED:
        return
    db.x("INSERT INTO triggered VALUES (?,?,?,?)", (zone_id, event_id, txh, time.time()))
    _sim_pay(zone_id, event_id, txh)


def _sim_pay(zone_id, event_id, txh):
    trig = db.one("SELECT ts FROM triggered WHERE zone_id=? AND event_id=?", (zone_id, event_id))
    cutoff = trig["ts"] - config.WAITING_PERIOD_S
    for h in db.q("SELECT * FROM households WHERE zone_id=? AND enrolled_at <= ? ORDER BY enrolled_at", (zone_id, cutoff)):
        if db.one("SELECT 1 FROM payouts WHERE id_hash=? AND event_id=?", (h["id_hash"], event_id)):
            continue
        bal = pool_balance_wei()
        if bal < config.PAYOUT_WEI:
            log.warning("sim pool empty, payouts paused for %s/%s", zone_id, event_id)
            return
        db.kv_set("sim_pool_wei", bal - config.PAYOUT_WEI)
        db.x("INSERT INTO payouts VALUES (?,?,?,?,?,?,?)",
             (h["id_hash"], zone_id, event_id, h["wallet"], str(config.PAYOUT_WEI), txh, time.time()))

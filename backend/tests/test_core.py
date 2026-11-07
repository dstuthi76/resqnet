"""Core tests - run from the backend folder:  python -m unittest discover tests -v"""
import base64
import collections
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["GEMINI_API_KEY"] = ""      # tests use the offline path
os.environ["CHAIN_MODE"] = "sim"

import chain  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import extract  # noqa: E402
import pipeline  # noqa: E402
import security  # noqa: E402


class ExtractionTests(unittest.TestCase):
    def test_multilingual(self):
        cases = {
            "3 people on roof, grandmother needs insulin": (3, "roof", "insulin", "elderly"),
            "4 మంది, మేడ మీద చిక్కుకుపోయాం, అమ్మమ్మకు ఇన్సులిన్ కావాలి": (4, "roof", "insulin", "elderly"),
            "5 பேர், கழுத்து வரை தண்ணீர், கர்ப்பிணி பெண் இருக்கிறார்": (5, "neck", None, "pregnant"),
            "2 लोग, कमर तक पानी है, एक आदमी घायल है": (2, "waist", "injury", None),
            "6 log chhat pe fase hai, dadi ko insulin chahiye": (6, "roof", "insulin", "elderly"),
        }
        for text, (people, water, med, vul) in cases.items():
            f = extract.rules_extract(text)
            self.assertTrue(f["is_sos"], text)
            self.assertEqual(f["people"], people, text)
            self.assertEqual(f["water_level"], water, text)
            if med:
                self.assertIn(med, f["medical"], text)
            if vul:
                self.assertIn(vul, f["vulnerable"], text)

    def test_noise(self):
        for t in ["Praying for everyone in Hyderabad 🙏 stay safe", "FWD: Heavy rain alert for next 48 hrs",
                  "Donate to flood relief here: bit.ly/xxxx", "సब लोग सुरक्षित रहें"]:
            self.assertFalse(extract.rules_extract(t)["is_sos"], t)

    def test_zone_from_text(self):
        self.assertEqual(extract.rules_extract("2 people stuck near Malakpet")["zone_id"], "HYD-MLK")
        self.assertEqual(extract.rules_extract("3 మంది, మలక్‌పేట్ దగ్గర సహాయం")["zone_id"], "HYD-MLK")


class DedupTests(unittest.TestCase):
    def test_synthetic_dataset(self):
        db.reset_all()
        pipeline.reset_cache()
        with open(os.path.join(config.DATA_DIR, "sos_messages.json"), encoding="utf-8") as f:
            recs = json.load(f)
        new, dup, _ = pipeline.store_raw(recs)
        self.assertGreater(len(dup), 0, "mesh re-deliveries must be dropped by id")
        pipeline.process(new)
        msgs = db.q("SELECT truth, case_id, is_sos FROM messages")
        incidents = {m["truth"] for m in msgs if m["truth"] != "NOISE"}
        cases = db.q("SELECT id FROM cases")
        by_case = collections.defaultdict(list)
        for m in msgs:
            if m["case_id"]:
                by_case[m["case_id"]].append(m["truth"])
        wrong_merges = sum(len(v) - collections.Counter(v).most_common(1)[0][1] for v in by_case.values())
        noise_as_sos = sum(1 for m in msgs if m["truth"] == "NOISE" and m["is_sos"])
        print(f"\n  {len(new)} messages -> {len(cases)} cases (truth {len(incidents)}), "
              f"wrong merges {wrong_merges}, noise kept {noise_as_sos}")
        self.assertLessEqual(len(cases), len(incidents) * 1.25)
        self.assertLessEqual(wrong_merges, len(new) * 0.03)
        self.assertEqual(noise_as_sos, 0)


class SignatureTests(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        db.reset_all()
        k = Ed25519PrivateKey.generate()
        pk = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        p = {"id": "sos-1", "lat": 17.373101, "lng": 78.500201, "ts": "2026-10-19T07:00:00.000Z", "people": 3,
             "needs": ["medicine", "boat"], "text": "3 people on roof, అమ్మమ్మకు ఇన్సులిన్", "device_id": "dev-1", "pk": pk}
        p["sig"] = base64.b64encode(k.sign(security.canonical(p).encode())).decode()
        self.assertEqual(security.verify(dict(p)), "valid")
        self.assertEqual(security.verify(dict(p, text="10 people")), "invalid")
        self.assertEqual(security.verify(dict(p, hops=5)), "valid")  # relays may change hops only
        other = Ed25519PrivateKey.generate()
        pk2 = base64.b64encode(other.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        q = dict(p, id="sos-2", pk=pk2)
        q["sig"] = base64.b64encode(other.sign(security.canonical(q).encode())).decode()
        self.assertEqual(security.verify(q), "key_mismatch")  # same device id, different key

    def test_canonical_format(self):
        self.assertEqual(security.canonical({"id": "a", "lat": None, "lng": 78.5, "ts": "t", "people": None,
                                             "needs": ["b", "a"], "text": "x", "device_id": "d"}),
                         "a||78.500000|t||a,b|x|d")


class ChainSimTests(unittest.TestCase):
    def setUp(self):
        db.reset_all()
        chain.ensure_ngos()
        chain.fund_pool(10 ** 18)
        for i in range(3):
            idh = chain.id_hash(f"h{i}", "1")
            w, _ = chain.new_wallet()
            db.x("INSERT INTO households VALUES (?,?,?,?,?,?,?)", (idh, "HYD-MLK", w, None, "t", time.time() - 10, "tx"))

    def paid(self):
        return db.one("SELECT COUNT(*) n FROM payouts")["n"]

    def test_rule(self):
        chain.submit_reading("HYD-MLK", "E1", 72)
        self.assertEqual(self.paid(), 0)
        chain.confirm_flood("ngo1", "HYD-MLK", "E1")
        self.assertEqual(self.paid(), 0)
        chain.confirm_flood("ngo2", "HYD-MLK", "E1")
        self.assertEqual(self.paid(), 3)
        chain.confirm_flood("ngo3", "HYD-MLK", "E1")
        chain.process_payouts("HYD-MLK", "E1")
        self.assertEqual(self.paid(), 3)  # once per household per event
        with self.assertRaises(ValueError):
            chain.confirm_flood("ngo1", "HYD-MLK", "E1")

    def test_below_threshold(self):
        chain.submit_reading("HYD-MLK", "E2", 24)
        chain.confirm_flood("ngo1", "HYD-MLK", "E2")
        chain.confirm_flood("ngo2", "HYD-MLK", "E2")
        self.assertEqual(self.paid(), 0)

    def test_late_enrolment_not_paid(self):
        chain.submit_reading("HYD-MLK", "E3", 72)
        chain.confirm_flood("ngo1", "HYD-MLK", "E3")
        chain.confirm_flood("ngo2", "HYD-MLK", "E3")
        db.x("INSERT INTO households VALUES (?,?,?,?,?,?,?)", ("late", "HYD-MLK", "0xlate", None, "t", time.time() + 5, "tx"))
        chain.process_payouts("HYD-MLK", "E3")
        self.assertEqual(self.paid(), 3)


if __name__ == "__main__":
    unittest.main()

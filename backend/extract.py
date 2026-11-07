"""Turn raw SOS text (any of Telugu / Tamil / Hindi / Hinglish / English) into
structured fields.

Primary path: Gemini with a strict JSON schema, in batches.
Fallback path: a multilingual keyword extractor that works fully offline, so
the demo never breaks when the free-tier quota runs out or there is no key.
"""
import logging
import re

import gemini
from zones import ZONES

log = logging.getLogger("resqnet.extract")

VULNERABLE = ["elderly", "child", "infant", "pregnant", "disabled"]
MEDICAL = ["insulin", "dialysis", "injury", "breathing", "fever", "other_medical"]
WATER_LEVELS = ["roof", "neck", "waist", "ground", "unknown"]
NEEDS = ["food", "water", "medicine", "boat"]

KW = {
    # vulnerability
    "elderly": ["grandmother", "grandfather", "grandma", "grandpa", "elderly", "old man", "old woman",
                "old lady", "senior citizen", "aged", "అమ్మమ్మ", "నానమ్మ", "తాతయ్య", "వృద్ధ", "பாட்டி",
                "தாத்தா", "முதிய", "दादी", "नानी", "दादा", "बुज़ुर्ग", "बुजुर्ग", "dadi", "nani", "buzurg", "budhe"],
    "child": ["kid", "child", "children", "baby", "infant", "months old", "పిల్ల", "పసిపాప", "குழந்தை",
              "बच्च", "bacch", "bache"],
    "infant": ["baby", "infant", "months old", "పసిపాప", "கைக்குழந்தை", "months ka", "1 साल"],
    "pregnant": ["pregnant", "గర్భిణ", "கர்ப்பிணி", "गर्भवती"],
    "disabled": ["wheelchair", "disabled", "handicap", "bedridden", "paralys", "विकलांग"],
    # medical
    "insulin": ["insulin", "diabetic", "sugar patient", "ఇన్సులిన్", "இன்சுலின்", "इंसुलिन"],
    "dialysis": ["dialysis", "డయాలసిస్", "டயாலிசிஸ்", "डायलिसिस"],
    "injury": ["injur", "bleeding", "wound", "fracture", "గాయ", "காயம்", "घायल", "ghayal", "khoon"],
    "breathing": ["breath", "asthma", "oxygen", "శ్వాస", "மூச்சு", "सांस", "saans"],
    "fever": ["fever", "జ్వరం", "காய்ச்சல்", "बुखार", "bukhar"],
    "other_medical": ["unconscious", "heart attack", "chest pain", "seizure", "बेहोश"],
    # water level (checked in severity order)
    "roof": ["roof", "terrace", "మేడ", "మిద్దె", "மாடி", "छत", "chhat", "chat pe"],
    "neck": ["neck", "chest", "మెడ", "கழுத்து", "गले", "gale", "seene"],
    "waist": ["waist", "knee", "నడుము", "இடுப்பு", "कमर", "kamar"],
    "ground": ["ground floor", "floor flooded", "అంతస్తు మునిగ", "தரைத்தளம்", "फ्लोर डूब", "doob", "house flooded"],
    # needs
    "food": ["food", "ఆహారం", "உணவு", "खाना", "khana"],
    "water": ["drinking water", "తాగునీ", "குடிநீர்", "पीने का पानी", "peene ka paani"],
    "medicine": ["medicine", "మందులు", "மருந்து", "दवा", "dawa"],
    "boat": ["boat", "బోట్", "படகு", "नाव", "naav"],
    # generic distress
    "stuck": ["stuck", "trapped", "rescue", "help", "fase", "फंसे", "बचाओ", "मदद", "చిక్కుకు", "కాపాడ",
              "సహాయం", "சிக்கி", "உதவ", "காப்பாற்ற"],
}
NOISE = ["fwd:", "forwarded", "pray", "donate", "video of", "is the ", "metro", "test test", "stay safe",
         "सुरक्षित रहें", "जाग्रत", "రక్షించు గాక", "జాగ్రత్తగా ఉండండి", "பாதுகாப்பாக இருங்கள்",
         "alert for next", "share with all", "bit.ly", "भगवान"]

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "ఇద్దరు": 2, "ముగ్గురు": 3, "నలుగురు": 4, "ఐదుగురు": 5, "ఆరుగురు": 6,
             "இரண்டு": 2, "மூன்று": 3, "நான்கு": 4, "ஐந்து": 5, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5,
             "do": 2, "teen": 3, "char": 4, "paanch": 5}
PEOPLE_RE = re.compile(
    r"(\d{1,3}|" + "|".join(map(re.escape, NUM_WORDS)) + r")\s*"
    r"(?:people|persons|ppl|members|family members|of us|log\b|लोग|మంది|పేర్|பேர்)",
    re.IGNORECASE)
FAMILY_RE = re.compile(r"family of (\d{1,2})", re.IGNORECASE)


def _has(text, key):
    return any(k in text for k in KW[key])


def detect_lang(text):
    counts = {"te": 0, "ta": 0, "hi": 0, "latin": 0}
    for ch in text:
        o = ord(ch)
        if 0x0C00 <= o <= 0x0C7F:
            counts["te"] += 1
        elif 0x0B80 <= o <= 0x0BFF:
            counts["ta"] += 1
        elif 0x0900 <= o <= 0x097F:
            counts["hi"] += 1
        elif ch.isascii() and ch.isalpha():
            counts["latin"] += 1
    top = max(counts, key=counts.get)
    if top == "latin":
        low = text.lower()
        hinglish = sum(w in low for w in [" hai", " log", " paani", " chahiye", " karo", " bhejo", " me ", " ke paas"])
        return "hi-Latn" if hinglish >= 1 else "en"
    return top


def find_zone(text):
    low = text.lower()
    for z in ZONES:
        for n in z["names"]:
            if n.lower() in low:
                return z["id"], z["name"]
    return None, None


def rules_extract(text):
    low = (text or "").lower()
    vulnerable = [k for k in VULNERABLE if _has(low, k)]
    if "infant" in vulnerable and "child" not in vulnerable:
        vulnerable.append("child")
    medical = [k for k in MEDICAL if _has(low, k)]
    water = next((w for w in ["roof", "neck", "waist", "ground"] if _has(low, w)), "unknown")
    needs = [k for k in NEEDS if _has(low, k)]
    if medical and "medicine" not in needs:
        needs.append("medicine")
    people = None
    m = PEOPLE_RE.search(text or "")
    if m:
        tok = m.group(1).lower()
        people = int(tok) if tok.isdigit() else NUM_WORDS.get(tok)
    else:
        m = FAMILY_RE.search(text or "")
        if m:
            people = int(m.group(1))
    zone_id, zone_name = find_zone(text or "")
    noise = any(n in low for n in NOISE)
    signal = water != "unknown" or bool(medical) or people is not None or _has(low, "stuck")
    is_sos = bool(signal and not noise)
    return {
        "is_sos": is_sos,
        "people": people,
        "vulnerable": vulnerable,
        "medical": medical,
        "water_level": water,
        "needs": needs,
        "location_text": zone_name,
        "zone_id": zone_id,
        "language": detect_lang(text or ""),
        "summary_en": summarize(people, water, vulnerable, medical, needs, zone_name) if is_sos else "",
    }


def summarize(people, water, vulnerable, medical, needs, place):
    bits = []
    bits.append(f"{people} people" if people else "Unknown number of people")
    bits.append({"roof": "on roof", "neck": "in neck-deep water", "waist": "in waist-deep water",
                 "ground": "ground floor flooded"}.get(water, "stranded"))
    if place:
        bits.append(f"near {place}")
    s = " ".join(bits)
    extra = []
    if vulnerable:
        extra.append(", ".join(vulnerable) + " present")
    if medical:
        extra.append("needs " + ", ".join(m.replace("_", " ") for m in medical))
    other = [n for n in needs if n != "medicine"]
    if other:
        extra.append("short of " + ", ".join(other))
    return s + ("; " + "; ".join(extra) if extra else "") + "."


# ---------------------------------------------------------------- Gemini path
SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "is_sos": {"type": "BOOLEAN"},
            "people": {"type": "INTEGER", "nullable": True},
            "vulnerable": {"type": "ARRAY", "items": {"type": "STRING", "enum": VULNERABLE}},
            "medical": {"type": "ARRAY", "items": {"type": "STRING", "enum": MEDICAL}},
            "water_level": {"type": "STRING", "enum": WATER_LEVELS},
            "needs": {"type": "ARRAY", "items": {"type": "STRING", "enum": NEEDS}},
            "location_text": {"type": "STRING", "nullable": True},
            "zone_id": {"type": "STRING", "nullable": True},
            "language": {"type": "STRING"},
            "summary_en": {"type": "STRING"},
        },
        "required": ["i", "is_sos", "vulnerable", "medical", "water_level", "needs", "language", "summary_en"],
    },
}

PROMPT = """You are the triage assistant of a flood rescue control room in Hyderabad, India.
Below are raw distress messages (Telugu, Tamil, Hindi, Hinglish or English; may contain typos).
For EACH message return one JSON object with:
- i: the message index
- is_sos: true only if someone needs rescue or urgent help now (not prayers, news forwards, ads, questions, tests)
- people: number of people needing help, or null if not stated
- vulnerable: any of elderly, child, infant, pregnant, disabled
- medical: any of insulin, dialysis, injury, breathing, fever, other_medical
- water_level: roof | neck | waist | ground | unknown  (roof = people have climbed onto roof/terrace)
- needs: any of food, water (drinking water), medicine, boat
- location_text: place/landmark mentioned, in English, or null
- zone_id: the best matching zone id from this list, or null: {zones}
- language: ISO code of the message language (te, ta, hi, hi-Latn, en)
- summary_en: one short English sentence a rescuer can act on
Do not invent facts that are not in the message.

Messages:
{messages}
"""


def gemini_extract_batch(texts):
    zones = ", ".join(f"{z['id']}={z['name']}" for z in ZONES)
    msgs = "\n".join(f"[{i}] {t}" for i, t in enumerate(texts))
    items = gemini.generate_json(PROMPT.format(zones=zones, messages=msgs), SCHEMA)
    out = {}
    for it in items:
        try:
            i = int(it["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < len(texts):
            out[i] = _clean(it)
    return out


def _clean(it):
    zid = it.get("zone_id")
    valid_zone = zid if zid in {z["id"] for z in ZONES} else None
    return {
        "is_sos": bool(it.get("is_sos")),
        "people": it.get("people") if isinstance(it.get("people"), int) and 0 < it["people"] < 500 else None,
        "vulnerable": [v for v in it.get("vulnerable", []) if v in VULNERABLE],
        "medical": [v for v in it.get("medical", []) if v in MEDICAL],
        "water_level": it.get("water_level") if it.get("water_level") in WATER_LEVELS else "unknown",
        "needs": [v for v in it.get("needs", []) if v in NEEDS],
        "location_text": it.get("location_text"),
        "zone_id": valid_zone,
        "language": it.get("language") or "",
        "summary_en": it.get("summary_en") or "",
    }


def extract_many(texts, batch_size=25):
    """Returns list of (fields, extractor_name)."""
    results = [None] * len(texts)
    if gemini.available():
        for start in range(0, len(texts), batch_size):
            chunk = texts[start:start + batch_size]
            try:
                got = gemini_extract_batch(chunk)
                for j, fields in got.items():
                    results[start + j] = (fields, "gemini")
            except gemini.GeminiError as e:
                log.warning("Gemini extraction failed, using rules for this batch: %s", e)
    for i, t in enumerate(texts):
        if results[i] is None:
            results[i] = (rules_extract(t), "rules")
    return results

"""Generate a realistic, messy synthetic SOS dataset for the Hyderabad demo.

~60 real incidents, each reported 1-8 times by different people, in Telugu,
Tamil, Hindi, Hinglish and English, through mesh / WhatsApp / SMS, with GPS
jitter, missing GPS, typos and mesh re-deliveries of the same packet. Also
some noise (prayers, news forwards, fake bursts).

Every record carries `_incident` (ground truth) so tests can measure how well
deduplication works. The backend ignores fields that start with "_".

Usage:  python data/generate_sos.py            -> data/sos_messages.json
"""
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from zones import ZONES  # noqa: E402

SEED = 42
N_INCIDENTS = 60
START = datetime(2020, 10, 14, 1, 0, tzinfo=timezone.utc)  # night of the Oct 2020 flood (IST 06:30)

AREA = {
    "en": {z["id"]: z["name"] for z in ZONES},
    "te": {z["id"]: next(n for n in z["names"] if "ఀ" <= n[0] <= "౿") for z in ZONES},
    "ta": {z["id"]: next(n for n in z["names"] if "஀" <= n[0] <= "௿") for z in ZONES},
    "hi": {z["id"]: next(n for n in z["names"] if "ऀ" <= n[0] <= "ॿ") for z in ZONES},
}
AREA["hinglish"] = AREA["en"]

# vulnerability / medical phrases per language
V = {
    "elderly": {"en": ["my grandmother is with us", "elderly couple here", "old man 75 yrs"],
                "te": ["అమ్మమ్మ కూడా ఉంది", "వృద్ధులు ఉన్నారు"],
                "ta": ["பாட்டி எங்களுடன் இருக்கிறார்", "முதியவர்கள் இருக்கிறார்கள்"],
                "hi": ["दादी भी साथ में हैं", "बुज़ुर्ग लोग हैं"],
                "hinglish": ["dadi bhi hai saath me", "buzurg log hai"]},
    "child": {"en": ["2 small kids", "children with us", "my baby is 8 months old"],
              "te": ["చిన్న పిల్లలు ఉన్నారు", "పసిపాప ఉంది"],
              "ta": ["குழந்தைகள் இருக்கிறார்கள்", "கைக்குழந்தை உள்ளது"],
              "hi": ["छोटे बच्चे हैं", "एक बच्चा 1 साल का है"],
              "hinglish": ["chote bacche hai", "baby hai 6 months ka"]},
    "pregnant": {"en": ["my wife is pregnant", "pregnant lady 8 months"],
                 "te": ["గర్భిణీ స్త్రీ ఉంది"],
                 "ta": ["கர்ப்பிணி பெண் இருக்கிறார்"],
                 "hi": ["गर्भवती महिला है"],
                 "hinglish": ["wife pregnant hai"]},
    "insulin": {"en": ["grandmother needs insulin urgently", "diabetic, insulin finished"],
                "te": ["అమ్మమ్మకు ఇన్సులిన్ కావాలి"],
                "ta": ["பாட்டிக்கு இன்சுலின் தேவை"],
                "hi": ["दादी को इंसुलिन चाहिए"],
                "hinglish": ["dadi ko insulin chahiye urgent"]},
    "injury": {"en": ["one person injured, bleeding", "father fell, leg injured"],
               "te": ["ఒకరికి గాయమైంది"],
               "ta": ["ஒருவருக்கு காயம்"],
               "hi": ["एक आदमी घायल है"],
               "hinglish": ["ek banda ghayal hai, khoon aa raha"]},
    "dialysis": {"en": ["father is a dialysis patient, missed session"],
                 "te": ["నాన్నకు డయాలసిస్ అవసరం"],
                 "ta": ["அப்பாவுக்கு டயாலிசிஸ் தேவை"],
                 "hi": ["पिताजी को डायलिसिस चाहिए"],
                 "hinglish": ["papa ka dialysis hai aaj"]},
    "breathing": {"en": ["asthma patient, breathing problem"],
                  "te": ["శ్వాస తీసుకోవడం కష్టంగా ఉంది"],
                  "ta": ["மூச்சு விட சிரமம்"],
                  "hi": ["सांस लेने में तकलीफ है"],
                  "hinglish": ["saans lene me problem hai"]},
    "fever": {"en": ["child has high fever"],
              "te": ["పిల్లాడికి జ్వరం"],
              "ta": ["குழந்தைக்கு காய்ச்சல்"],
              "hi": ["बच्चे को बुखार है"],
              "hinglish": ["bacche ko bukhar hai"]},
}
WATER = {
    "roof": {"en": "we are on the roof", "te": "మేడ మీద చిక్కుకుపోయాం", "ta": "மாடியில் சிக்கியுள்ளோம்",
             "hi": "छत पर फंसे हैं", "hinglish": "chhat pe fase hai"},
    "neck": {"en": "water up to neck level", "te": "నీళ్లు మెడ వరకు వచ్చాయి", "ta": "கழுத்து வரை தண்ணீர்",
             "hi": "गले तक पानी है", "hinglish": "gale tak paani hai"},
    "waist": {"en": "water at waist level and rising", "te": "నడుము వరకు నీళ్లు", "ta": "இடுப்பு வரை தண்ணீர்",
              "hi": "कमर तक पानी है", "hinglish": "kamar tak paani"},
    "ground": {"en": "ground floor flooded", "te": "కింద అంతస్తు మునిగిపోయింది", "ta": "தரைத்தளம் மூழ்கியது",
               "hi": "नीचे का फ्लोर डूब गया", "hinglish": "ground floor doob gaya"},
}
NEED = {
    "food": {"en": "no food since yesterday", "te": "నిన్నటి నుంచి ఆహారం లేదు", "ta": "நேற்று முதல் உணவு இல்லை",
             "hi": "कल से खाना नहीं है", "hinglish": "kal se khana nahi"},
    "water": {"en": "need drinking water", "te": "తాగునీరు కావాలి", "ta": "குடிநீர் தேவை",
              "hi": "पीने का पानी चाहिए", "hinglish": "peene ka paani chahiye"},
}
HELP = {"en": ["Please help!!", "PLEASE SEND BOAT", "help pls", "Urgent rescue needed"],
        "te": ["దయచేసి కాపాడండి!", "సహాయం చేయండి"],
        "ta": ["உதவுங்கள்!", "தயவுசெய்து காப்பாற்றுங்கள்"],
        "hi": ["कृपया मदद करें!", "जल्दी बचाओ"],
        "hinglish": ["please help karo", "boat bhejo jaldi"]}
PEOPLE = {"en": "{n} people", "te": "{n} మంది", "ta": "{n} பேர்", "hi": "{n} लोग", "hinglish": "{n} log"}
NEAR = {"en": "near {a}", "te": "{a} దగ్గర", "ta": "{a} அருகில்", "hi": "{a} के पास", "hinglish": "{a} ke paas"}

NOISE = [
    ("en", "Praying for everyone in Hyderabad 🙏 stay safe"),
    ("en", "FWD: Heavy rain alert for next 48 hrs, share with all groups"),
    ("en", "Is the Old City road open? need to go to office"),
    ("hi", "सब लोग सुरक्षित रहें, भगवान सबकी रक्षा करे"),
    ("te", "అందరూ జాగ్రత్తగా ఉండండి"),
    ("ta", "அனைவரும் பாதுகாப்பாக இருங்கள்"),
    ("hinglish", "koi bata sakta hai metro chal rahi hai kya"),
    ("en", "test test"),
    ("en", "Video of flooded street in Tolichowki, see how bad it is"),
    ("en", "Donate to flood relief here: bit.ly/xxxx"),
]

LANG_WEIGHTS = [("en", 0.30), ("te", 0.30), ("hinglish", 0.15), ("hi", 0.12), ("ta", 0.13)]


def pick_lang(rng):
    r, acc = rng.random(), 0.0
    for lang, w in LANG_WEIGHTS:
        acc += w
        if r <= acc:
            return lang
    return "en"


def jitter(rng, lat, lng, metres):
    ang = rng.random() * 2 * math.pi
    d = rng.random() * metres
    return (lat + (d * math.cos(ang)) / 111000.0,
            lng + (d * math.sin(ang)) / (111000.0 * math.cos(math.radians(lat))))


def make_incidents(rng):
    incidents = []
    for i in range(N_INCIDENTS):
        z = rng.choice(ZONES[:8] if rng.random() < 0.8 else ZONES)
        while True:
            lat, lng = jitter(rng, z["lat"], z["lng"], 900)
            if all(abs(lat - o["lat"]) + abs(lng - o["lng"]) > 0.002 for o in incidents):
                break
        vuln = [k for k, p in [("elderly", .35), ("child", .35), ("pregnant", .1)] if rng.random() < p]
        med = [k for k, p in [("insulin", .1), ("injury", .1), ("dialysis", .04),
                              ("breathing", .06), ("fever", .08)] if rng.random() < p]
        incidents.append({
            "incident": f"INC-{i:03d}", "zone": z["id"], "lat": lat, "lng": lng,
            "people": rng.choice([1, 2, 3, 3, 4, 4, 5, 6, 8, 12]),
            "vuln": vuln, "med": med,
            "water": rng.choices(["roof", "neck", "waist", "ground"], [.35, .15, .3, .2])[0],
            "need": [k for k in ("food", "water") if rng.random() < .3],
            "start": START + timedelta(minutes=rng.randint(0, 360)),
        })
    return incidents


def render(rng, inc, lang, with_area):
    parts = []
    people = PEOPLE[lang].format(n=inc["people"])
    parts.append(f"{people}, {WATER[inc['water']][lang]}")
    if with_area:
        parts.append(NEAR[lang].format(a=AREA[lang][inc["zone"]]))
    for k in inc["vuln"] + inc["med"]:
        if rng.random() < 0.85:  # reporters forget details
            parts.append(rng.choice(V[k][lang]))
    for k in inc["need"]:
        parts.append(NEED[k][lang])
    parts.append(rng.choice(HELP[lang]))
    sep = rng.choice([". ", ", ", " "])
    text = sep.join(parts)
    if lang in ("en", "hinglish") and rng.random() < 0.25:
        text = text.upper() if rng.random() < 0.5 else text.lower()
    return text


def main(out_path):
    rng = random.Random(SEED)
    incidents = make_incidents(rng)
    records, n = [], 0
    for inc in incidents:
        k = rng.choices([1, 2, 3, 4, 5, 6, 8], [.12, .18, .2, .18, .14, .1, .08])[0]
        for _ in range(k):
            n += 1
            lang = pick_lang(rng)
            channel = rng.choices(["mesh", "whatsapp", "sms"], [.45, .4, .15])[0]
            has_gps = channel == "mesh" or rng.random() < 0.75
            lat, lng = jitter(rng, inc["lat"], inc["lng"], 40) if has_gps else (None, None)
            rec = {
                "id": f"msg-{n:04d}",
                "channel": channel,
                "text": render(rng, inc, lang, with_area=(not has_gps) or rng.random() < 0.5),
                "lat": round(lat, 6) if lat else None,
                "lng": round(lng, 6) if lng else None,
                "ts": (inc["start"] + timedelta(minutes=rng.randint(0, 240))).isoformat(),
                "hops": rng.randint(1, 4) if channel == "mesh" else 0,
                "device_id": f"dev-{rng.randint(1000, 9999)}",
                "_incident": inc["incident"],
                "_lang": lang,
            }
            records.append(rec)
            if channel == "mesh" and rng.random() < 0.3:  # same packet delivered by another relay
                dup = dict(rec, hops=rec["hops"] + rng.randint(1, 2))
                records.append(dup)
    for lang, text in NOISE * 2:
        n += 1
        records.append({"id": f"msg-{n:04d}", "channel": "whatsapp", "text": text,
                        "lat": None, "lng": None,
                        "ts": (START + timedelta(minutes=rng.randint(0, 400))).isoformat(),
                        "hops": 0, "device_id": f"dev-{rng.randint(1000, 9999)}",
                        "_incident": "NOISE", "_lang": lang})
    rng.shuffle(records)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)
    uniq = len({r["id"] for r in records})
    print(f"wrote {len(records)} records ({uniq} unique ids) from {len(incidents)} incidents -> {out_path}")


if __name__ == "__main__":
    main(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sos_messages.json"))

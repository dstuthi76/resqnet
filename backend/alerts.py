"""Early warning: Open-Meteo rainfall forecast -> zone risk -> multilingual alert text (+ audio)."""
import io
import logging

import requests

import gemini
from zones import ZONES

log = logging.getLogger("resqnet.alerts")

LANGS = {"te": "Telugu", "ta": "Tamil", "hi": "Hindi", "en": "English"}
HEAVY_MM = 64.5   # IMD "heavy rain" threshold for 24 h
VERY_HEAVY_MM = 115.6


def forecast():
    """3-day rainfall forecast per zone. Falls back to a labelled demo scenario if offline."""
    lats = ",".join(str(z["lat"]) for z in ZONES)
    lngs = ",".join(str(z["lng"]) for z in ZONES)
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
            "latitude": lats, "longitude": lngs, "daily": "precipitation_sum",
            "forecast_days": 3, "timezone": "Asia/Kolkata"})
        r.raise_for_status()
        data = r.json()
        data = data if isinstance(data, list) else [data]
        out = []
        for z, d in zip(ZONES, data):
            mm = [x or 0 for x in d["daily"]["precipitation_sum"]]
            out.append(_risk(z, mm, d["daily"]["time"]))
        return {"source": "open-meteo", "zones": out}
    except Exception as e:  # network blocked, API down...
        log.warning("Open-Meteo unavailable (%s); using demo scenario", e)
        demo = {"HYD-MLK": [192, 98, 30], "HYD-CYG": [180, 90, 25], "HYD-FLK": [170, 85, 20],
                "HYD-TLC": [160, 70, 15], "HYD-MDP": [150, 60, 15], "HYD-LBN": [185, 95, 30],
                "HYD-SRN": [175, 88, 22], "HYD-UPL": [120, 50, 10], "HYD-HYT": [110, 45, 10],
                "HYD-MRB": [190, 96, 28]}
        days = ["2020-10-13", "2020-10-14", "2020-10-15"]
        return {"source": "demo (Oct 2020 scenario - Open-Meteo unreachable)",
                "zones": [_risk(z, demo[z["id"]], days) for z in ZONES]}


def _risk(z, mm, days):
    peak = max(mm)
    if peak >= VERY_HEAVY_MM or (peak >= HEAVY_MM and z["low_lying"]):
        level = "high"
    elif peak >= HEAVY_MM or (peak >= 35 and z["low_lying"]):
        level = "medium"
    else:
        level = "low"
    return {"zoneId": z["id"], "name": z["name"], "lat": z["lat"], "lng": z["lng"], "lowLying": z["low_lying"],
            "rain_mm": mm, "days": days, "peak_mm": peak, "risk": level}


TEMPLATES = {
    "en": "Flood warning for {areas}. Very heavy rain is expected in the next 24 hours. Move documents, medicines "
          "and food to an upper floor now. Switch off the main power if water enters your home. If you are told to "
          "leave, go to {shelter}. Keep your phone charged and the ResQNet app open - it can send SOS even without network.",
    "te": "{areas} ప్రాంతాలకు వరద హెచ్చరిక. రాబోయే 24 గంటల్లో అతి భారీ వర్షం కురిసే అవకాశం ఉంది. పత్రాలు, మందులు, ఆహారం "
          "ఇప్పుడే పై అంతస్తుకు తరలించండి. ఇంట్లోకి నీరు వస్తే మెయిన్ కరెంట్ ఆపేయండి. ఖాళీ చేయమని చెబితే {shelter} కు వెళ్ళండి. "
          "ఫోన్ ఛార్జ్ చేసి ఉంచండి, నెట్‌వర్క్ లేకపోయినా ResQNet యాప్ ద్వారా SOS పంపవచ్చు.",
    "ta": "{areas} பகுதிகளுக்கு வெள்ள எச்சரிக்கை. அடுத்த 24 மணி நேரத்தில் மிக கனமழை எதிர்பார்க்கப்படுகிறது. ஆவணங்கள், "
          "மருந்துகள், உணவை இப்போதே மேல் தளத்திற்கு மாற்றுங்கள். வீட்டிற்குள் தண்ணீர் வந்தால் மின்சாரத்தை அணைக்கவும். "
          "வெளியேறச் சொன்னால் {shelter} செல்லுங்கள். நெட்வொர்க் இல்லாவிட்டாலும் ResQNet செயலி மூலம் SOS அனுப்பலாம்.",
    "hi": "{areas} के लिए बाढ़ की चेतावनी। अगले 24 घंटों में बहुत भारी बारिश की संभावना है। दस्तावेज़, दवाइयाँ और खाना अभी "
          "ऊपर की मंज़िल पर रखें। घर में पानी आए तो मेन बिजली बंद कर दें। निकलने को कहा जाए तो {shelter} जाएँ। फ़ोन चार्ज रखें, "
          "नेटवर्क न होने पर भी ResQNet ऐप से SOS भेज सकते हैं।",
}


SCRIPT = {"te": (0x0C00, 0x0C7F), "ta": (0x0B80, 0x0BFF), "hi": (0x0900, 0x097F)}


def local_name(z, lang):
    if lang in SCRIPT:
        lo, hi = SCRIPT[lang]
        for n in z["names"]:
            if lo <= ord(n[0]) <= hi:
                return n
    return z["name"]


def generate(lang, zone_ids, shelter):
    lang = lang if lang in LANGS else "en"
    chosen = [z for z in ZONES if z["id"] in zone_ids] or ZONES[:3]
    names = [local_name(z, lang) for z in chosen]
    areas = ", ".join(names)
    if gemini.available():
        prompt = (f"Write a short spoken flood warning (max 70 words) in {LANGS[lang]} for residents of {areas}, "
                  f"Hyderabad. Very heavy rain is expected in the next 24 hours. Tell them: move documents, medicines "
                  f"and food upstairs; switch off mains if water enters; go to {shelter} if asked to evacuate; keep "
                  f"the phone charged and the ResQNet app open because it can send SOS without network. Use simple "
                  f"everyday words, no markdown, no English unless the language is English. Output only the message.")
        try:
            return {"text": gemini.generate_text(prompt), "source": "gemini", "lang": lang}
        except gemini.GeminiError as e:
            log.warning("Gemini alert failed: %s", e)
    return {"text": TEMPLATES[lang].format(areas=areas, shelter=shelter), "source": "template", "lang": lang}


def tts_mp3(text, lang):
    """MP3 bytes via gTTS (free, needs internet). Returns None if unavailable;
    the dashboard then falls back to the browser's built-in speech."""
    try:
        from gtts import gTTS
    except ImportError:
        return None
    try:
        buf = io.BytesIO()
        gTTS(text=text, lang=lang if lang in LANGS else "en").write_to_fp(buf)
        return buf.getvalue()
    except Exception as e:
        log.warning("gTTS failed: %s", e)
        return None

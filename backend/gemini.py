"""Tiny Gemini REST client (Google AI Studio free-tier key). No SDK needed.

All functions raise GeminiError on any failure so callers can fall back to
the offline rule-based path.
"""
import json
import time

import requests

import config

BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(Exception):
    pass


def available():
    return bool(config.GEMINI_API_KEY)


def _post(path, body, timeout=60, retries=3):
    if not available():
        raise GeminiError("GEMINI_API_KEY not set")
    url = f"{BASE}/{path}"
    headers = {"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:  # free-tier rate limit / transient
                last = f"{r.status_code}: {r.text[:200]}"
                time.sleep(2 ** attempt * 3)
                continue
            if r.status_code != 200:
                raise GeminiError(f"{r.status_code}: {r.text[:300]}")
            return r.json()
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2 ** attempt)
    raise GeminiError(f"Gemini failed after retries: {last}")


def generate_json(prompt, schema, temperature=0.1):
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    data = _post(f"models/{config.GEMINI_MODEL}:generateContent", body, timeout=120)
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, ValueError) as e:
        raise GeminiError(f"bad response: {e}: {str(data)[:300]}")


def generate_text(prompt, temperature=0.4):
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature}}
    data = _post(f"models/{config.GEMINI_MODEL}:generateContent", body)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        raise GeminiError(f"bad response: {e}")


def embed(texts):
    """Return one embedding vector per text (multilingual)."""
    model = f"models/{config.GEMINI_EMBED_MODEL}"
    out = []
    for i in range(0, len(texts), 100):
        chunk = texts[i:i + 100]
        body = {"requests": [{"model": model, "content": {"parts": [{"text": t}]},
                              "taskType": "SEMANTIC_SIMILARITY"} for t in chunk]}
        data = _post(f"{model}:batchEmbedContents", body)
        try:
            out.extend(e["values"] for e in data["embeddings"])
        except KeyError as e:
            raise GeminiError(f"bad embed response: {e}")
    return out

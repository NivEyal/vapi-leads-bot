"""
AI Sales Call Bot
=================
Flow:
  1. /voice           → מנגן ברכה (Grok TTS) + מקליט (Twilio Record)
  2. /recording       → Grok STT → detect_interest → WhatsApp + Sheet
  3. /recording-retry → ניסיון שני אם לא זוהה עניין

כל TTS = Grok. כל STT = Grok. Twilio רק מנגן + מקליט.
"""

from flask import Flask, request, Response, jsonify
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from google.oauth2.service_account import Credentials
import gspread
import requests
import os
import re
import uuid
import json
import threading
from datetime import datetime

app = Flask(__name__)

# =========================
# Config
# =========================
def env(name, default=""):
    return os.getenv(name, default).strip()

PUBLIC_BASE_URL      = env("PUBLIC_BASE_URL").rstrip("/")
TWILIO_ACCOUNT_SID   = env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN    = env("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = env("TWILIO_WHATSAPP_FROM")
XAI_API_KEY          = env("XAI_API_KEY")
GOOGLE_SHEETS_ID     = env("GOOGLE_SHEETS_ID")
GROK_TTS_VOICE       = env("GROK_TTS_VOICE", "leo")
GROK_TTS_LANGUAGE    = env("GROK_TTS_LANGUAGE", "he")

AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

EXPECTED_HEADERS = [
    "timestamp", "call_sid", "caller", "user_text", "interested",
    "sentiment", "lead_quality", "next_action", "summary",
    "whatsapp_sent", "whatsapp_sid", "stt_error", "openai_error", "tts_error",
]

# =========================
# Twilio
# =========================
twilio_client = None
try:
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        print("✅ Twilio connected", flush=True)
except Exception as e:
    print("🔥 TWILIO ERROR:", e, flush=True)

# =========================
# Google Sheets
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_google_info = {
    "type": env("GOOGLE_TYPE"),
    "project_id": env("GOOGLE_PROJECT_ID"),
    "private_key_id": env("GOOGLE_PRIVATE_KEY_ID"),
    "private_key": os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n").strip(),
    "client_email": env("GOOGLE_CLIENT_EMAIL"),
    "client_id": env("GOOGLE_CLIENT_ID"),
    "auth_uri": env("GOOGLE_AUTH_URI"),
    "token_uri": env("GOOGLE_TOKEN_URI"),
    "auth_provider_x509_cert_url": env("GOOGLE_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": env("GOOGLE_CLIENT_X509_CERT_URL"),
    "universe_domain": env("GOOGLE_UNIVERSE_DOMAIN", "googleapis.com"),
}

sheet = None
try:
    _creds = Credentials.from_service_account_info(_google_info, scopes=SCOPES)
    _gs = gspread.authorize(_creds)
    sheet = _gs.open_by_key(GOOGLE_SHEETS_ID).sheet1
    print("✅ Google Sheets connected", flush=True)
except Exception as e:
    print("🔥 GOOGLE SHEETS ERROR:", e, flush=True)

# =========================
# Helpers
# =========================
def now_iso():
    return datetime.utcnow().isoformat()

def ensure_headers():
    if sheet is None:
        return
    try:
        if sheet.row_values(1) != EXPECTED_HEADERS:
            sheet.update("A1:N1", [EXPECTED_HEADERS])
    except Exception as e:
        print("🔥 HEADER FIX ERROR:", e, flush=True)

def append_lead_row(row):
    if sheet is None:
        return
    ensure_headers()
    try:
        sheet.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print("🔥 SHEET APPEND ERROR:", e, flush=True)

def get_sheet_rows():
    if sheet is None:
        return []
    ensure_headers()
    try:
        return sheet.get_all_records(expected_headers=EXPECTED_HEADERS)
    except Exception as e:
        print("🔥 GET ROWS ERROR:", e, flush=True)
        return []

def normalize_phone(phone):
    if not phone:
        return ""
    if phone.startswith("whatsapp:"):
        return phone
    digits = re.sub(r"[^\d+]", "", phone)
    if digits.startswith("0"):
        digits = "+972" + digits[1:]
    elif digits.startswith("972"):
        digits = "+" + digits
    elif not digits.startswith("+"):
        digits = "+" + digits
    return f"whatsapp:{digits}"

def safe_json_loads(text):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}

# =========================
# Grok TTS  (עם cache)
# =========================
_tts_cache: dict = {}

def grok_tts(text: str) -> str:
    """מחזיר URL ציבורי לקובץ MP3. שומר cache."""
    if text in _tts_cache:
        url = _tts_cache[text]
        fname = url.split("/")[-1]
        if os.path.exists(os.path.join(AUDIO_DIR, fname)):
            print(f"🎵 TTS CACHE HIT: {text[:40]}", flush=True)
            return url

    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY missing")

    res = requests.post(
        "https://api.x.ai/v1/tts",
        headers={
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "text": text[:1500],
            "voice_id": GROK_TTS_VOICE,
            "language": GROK_TTS_LANGUAGE,
            "format": "mp3",
        },
        timeout=20,
    )
    print(f"🔊 GROK TTS {res.status_code} | {len(res.content)} bytes", flush=True)
    if res.status_code != 200:
        raise RuntimeError(f"Grok TTS {res.status_code}: {res.text[:200]}")

    fname = f"{uuid.uuid4()}.mp3"
    path = os.path.join(AUDIO_DIR, fname)
    with open(path, "wb") as f:
        f.write(res.content)

    url = f"{PUBLIC_BASE_URL}/audio/{fname}"
    _tts_cache[text] = url
    return url


@app.get("/audio/<fname>")
def serve_audio(fname):
    path = os.path.join(AUDIO_DIR, fname)
    if not os.path.exists(path):
        return Response("not found", status=404)
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# =========================
# Grok STT
# =========================
def grok_stt(recording_url: str) -> str:
    """מוריד הקלטה מ-Twilio → Grok STT → טקסט."""
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY missing")

    audio = requests.get(
        recording_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=20,
    )
    if audio.status_code != 200:
        raise RuntimeError(f"Download recording failed: {audio.status_code}")

    res = requests.post(
        "https://api.x.ai/v1/stt",
        headers={"Authorization": f"Bearer {XAI_API_KEY}"},
        files={"file": ("rec.wav", audio.content, "audio/wav")},
        timeout=30,
    )
    print(f"🎤 GROK STT {res.status_code}", flush=True)
    if res.status_code != 200:
        raise RuntimeError(f"Grok STT {res.status_code}: {res.text[:200]}")

    text = res.json().get("text", "").strip()
    print(f"🎤 GROK STT TEXT: {repr(text)}", flush=True)
    return text


# =========================
# WhatsApp
# =========================
def send_whatsapp(to_phone: str, summary: str = "") -> str:
    if not twilio_client:
        raise RuntimeError("Twilio not initialized")
    if not TWILIO_WHATSAPP_FROM:
        raise RuntimeError("TWILIO_WHATSAPP_FROM missing")
    msg = twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=normalize_phone(to_phone),
        body=(
            "היי, תודה על השיחה 🙏\n\n"
            "כמו שביקשת, הנה הפרטים להמשך:\n"
            f"{summary}\n\n"
            "נשמח לעזור ולתאם המשך."
        ),
    )
    return msg.sid


# =========================
# Interest Detection
# =========================
YES_WORDS = [
    # עברית
    "כן", "בטח", "ברור", "אפשר", "יאללה", "יאלה", "סבבה", "אוקיי", "אוקי",
    "שלח", "תשלח", "תשלחי", "שלחו", "וואטסאפ", "ווטסאפ", "ווצאפ",
    "מעוניין", "מעוניינת", "רוצה", "אשמח", "פרטים", "יכול", "נשמח", "קדימה",
    # אנגלית
    "yes", "yeah", "yep", "ok", "okay", "sure", "please", "send",
]

NO_WORDS = [
    "לא מעוניין", "לא מעוניינת", "לא רלוונטי", "לא תודה",
    "אל תשלח", "לא לשלוח", "תוריד", "עזוב",
    "no thanks", "not interested", "not now",
]

def detect_interest(text: str) -> bool:
    if not text:
        return False
    clean = text.strip().lower()
    clean = re.sub(r"[^\w\u0590-\u05FF\s]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    print(f"🔍 DETECT: '{clean}'", flush=True)

    for phrase in NO_WORDS:
        if phrase in clean:
            print(f"❌ NO: '{phrase}'", flush=True)
            return False

    if re.search(r"(?<!\w)לא(?!\w)", clean):
        strong = ["כן","בטח","ברור","אשמח","רוצה","yes","ok","okay","sure"]
        if not any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", clean) for w in strong):
            print("❌ Standalone לא", flush=True)
            return False

    for word in YES_WORDS:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", clean):
            print(f"✅ YES: '{word}'", flush=True)
            return True

    print("⚪ No match", flush=True)
    return False


# =========================
# Static texts (קצרים = TTS מהיר)
# =========================
GREETING_TEXT     = "שלום! רוצה שאשלח לך פרטים בוואטסאפ? תגיד כן."
SUCCESS_TEXT      = "מעולה! שלחתי לך פרטים בוואטסאפ. תודה ולהתראות!"
RETRY_RECORD_TEXT = "לא קלטתי. תגיד כן אם תרצה שאשלח לך פרטים."
FINAL_TEXT        = "תודה ולהתראות."


def prewarm_tts():
    if not XAI_API_KEY:
        print("⚠️ Skipping TTS prewarm - no XAI_API_KEY", flush=True)
        return
    for text in [GREETING_TEXT, SUCCESS_TEXT, RETRY_RECORD_TEXT, FINAL_TEXT]:
        try:
            grok_tts(text)
            print(f"🎵 Pre-warmed: {text[:50]}", flush=True)
        except Exception as e:
            print(f"⚠️ Prewarm failed: {e}", flush=True)


def _play_or_say(r, text: str):
    try:
        r.play(grok_tts(text))
    except Exception as e:
        print(f"⚠️ TTS fallback to say: {e}", flush=True)
        r.say(text, language="he-IL")


# =========================
# Core handler
# =========================
def _handle_recording(caller, call_sid, recording_url, is_retry=False):
    user_text  = ""
    stt_error  = ""
    wa_sent    = "no"
    wa_sid     = ""

    # ── Grok STT ──
    try:
        if recording_url:
            user_text = grok_stt(recording_url + ".wav")
    except Exception as e:
        stt_error = str(e)[:200]
        print(f"🔥 STT ERROR: {stt_error}", flush=True)

    interested = detect_interest(user_text)
    print(f"📊 INTERESTED={interested} retry={is_retry} caller={caller}", flush=True)

    summary = "הלקוח ביקש לקבל פרטים בוואטסאפ." if interested else "לא זוהה עניין ברור."

    # ── WhatsApp ──
    if interested and caller:
        try:
            wa_sid  = send_whatsapp(caller, "נשמח להמשך שיחה ותיאום.")
            wa_sent = "yes"
            print(f"✅ WhatsApp → {caller} | {wa_sid}", flush=True)
        except Exception as e:
            wa_sent = f"failed:{str(e)[:100]}"
            print(f"🔥 WA ERROR: {e}", flush=True)

    # ── Sheet (async) ──
    row = [
        now_iso(),
        call_sid + ("_retry" if is_retry else ""),
        caller, user_text,
        "yes" if interested else "no",
        "positive" if interested else "neutral",
        "hot" if interested else "cold",
        "send_whatsapp" if interested else "no_action",
        summary, wa_sent, wa_sid, stt_error, "", "",
    ]
    threading.Thread(target=append_lead_row, args=(row,), daemon=True).start()

    # ── TwiML ──
    r = VoiceResponse()

    if interested:
        _play_or_say(r, SUCCESS_TEXT)
        r.hangup()

    elif is_retry:
        _play_or_say(r, FINAL_TEXT)
        r.hangup()

    else:
        # ניסיון שני: הקלטה נוספת
        _play_or_say(r, RETRY_RECORD_TEXT)
        r.record(
            action="/recording-retry",
            method="POST",
            max_length=8,
            timeout=5,
            play_beep=False,
            transcribe=False,
        )
        _play_or_say(r, FINAL_TEXT)
        r.hangup()

    return Response(str(r), mimetype="text/xml")


# =========================
# Routes
# =========================

@app.post("/voice")
def voice():
    caller   = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")
    print(f"📞 Incoming: {caller} | {call_sid}", flush=True)

    r = VoiceResponse()
    _play_or_say(r, GREETING_TEXT)

    r.record(
        action="/recording",
        method="POST",
        max_length=8,
        timeout=5,
        play_beep=False,
        transcribe=False,
    )

    _play_or_say(r, FINAL_TEXT)
    r.hangup()
    return Response(str(r), mimetype="text/xml")


@app.post("/recording")
def recording():
    caller        = request.form.get("From", "")
    call_sid      = request.form.get("CallSid", "")
    recording_url = request.form.get("RecordingUrl", "")
    print(f"🎙️ Recording | {caller} | {recording_url}", flush=True)
    return _handle_recording(caller, call_sid, recording_url, is_retry=False)


@app.post("/recording-retry")
def recording_retry():
    caller        = request.form.get("From", "")
    call_sid      = request.form.get("CallSid", "")
    recording_url = request.form.get("RecordingUrl", "")
    print(f"🎙️ Retry | {caller}", flush=True)
    return _handle_recording(caller, call_sid, recording_url, is_retry=True)


# =========================
# Test & Utility
# =========================

@app.get("/test-whatsapp")
def test_whatsapp():
    to = request.args.get("to", "").strip()
    if not to:
        return jsonify({"ok": False, "error": "missing ?to=+972..."})
    try:
        sid = send_whatsapp(to, "זו הודעת בדיקה מהמערכת.")
        return jsonify({"ok": True, "sid": sid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/test-detect")
def test_detect():
    text = request.args.get("text", "").strip()
    return jsonify({"text": text, "interested": detect_interest(text)})

@app.get("/test-tts")
def test_tts():
    text = request.args.get("text", GREETING_TEXT).strip()
    try:
        url = grok_tts(text)
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================
# Stats + Dashboard
# =========================

@app.get("/stats")
def stats():
    rows = get_sheet_rows()
    total      = len(rows)
    interested = sum(1 for r in rows if str(r.get("interested","")).lower() == "yes")
    wa_sent    = sum(1 for r in rows if str(r.get("whatsapp_sent","")).lower().startswith("yes"))
    return jsonify({
        "ok": True, "total_calls": total, "interested": interested,
        "whatsapp_sent": wa_sent,
        "conversion_pct": round(interested / total * 100, 2) if total else 0,
    })

@app.get("/dashboard")
def dashboard():
    rows       = get_sheet_rows()
    total      = len(rows)
    interested = sum(1 for r in rows if str(r.get("interested","")).lower() == "yes")
    wa_sent    = sum(1 for r in rows if str(r.get("whatsapp_sent","")).lower().startswith("yes"))
    conversion = round(interested / total * 100, 2) if total else 0
    recent     = list(reversed(rows[-30:]))

    table_rows = ""
    for r in recent:
        ib = "🟢 כן" if str(r.get("interested","")).lower() == "yes" else "🔴 לא"
        wb = "🟢 נשלח" if str(r.get("whatsapp_sent","")).lower().startswith("yes") else "⚪ לא"
        table_rows += f"""<tr>
            <td>{r.get("timestamp","")}</td><td>{r.get("caller","")}</td>
            <td>{ib}</td><td>{wb}</td><td>{r.get("lead_quality","")}</td>
            <td>{r.get("sentiment","")}</td><td>{r.get("summary","")}</td>
            <td>{r.get("user_text","")}</td></tr>"""

    html = f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><title>AI Calls Dashboard</title>
<style>
body{{font-family:Arial;background:#f7f7f7;padding:24px}}
.cards{{display:flex;gap:16px;flex-wrap:wrap}}
.card{{background:white;padding:18px;border-radius:12px;min-width:180px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
.num{{font-size:30px;font-weight:bold}}
table{{margin-top:24px;width:100%;border-collapse:collapse;background:white}}
th,td{{padding:10px;border-bottom:1px solid #eee;vertical-align:top}}
th{{background:#111;color:white}}
</style></head><body>
<h1>דשבורד שיחות AI</h1>
<div class="cards">
  <div class="card"><div>סה״כ שיחות</div><div class="num">{total}</div></div>
  <div class="card"><div>מתעניינים</div><div class="num">{interested}</div></div>
  <div class="card"><div>וואטסאפ נשלח</div><div class="num">{wa_sent}</div></div>
  <div class="card"><div>המרה</div><div class="num">{conversion}%</div></div>
</div>
<table><tr>
  <th>זמן</th><th>טלפון</th><th>עניין</th><th>וואטסאפ</th>
  <th>איכות</th><th>סנטימנט</th><th>סיכום</th><th>מה אמר</th>
</tr>
{table_rows or '<tr><td colspan="8">אין שיחות עדיין</td></tr>'}
</table></body></html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


# =========================
# Health
# =========================

@app.get("/")
def home():
    ensure_headers()
    return jsonify({
        "ok": True,
        "service": "AI sales call bot (Grok STT+TTS)",
        "flow": "/voice → Twilio Record → /recording → Grok STT → detect → WhatsApp",
        "routes": ["/voice", "/dashboard", "/stats",
                   "/test-whatsapp?to=+972...",
                   "/test-detect?text=כן",
                   "/test-tts?text=שלום"],
    })

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


# =========================
# Startup
# =========================
with app.app_context():
    prewarm_tts()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

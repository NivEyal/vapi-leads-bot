from flask import Flask, request, Response, jsonify
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from google.oauth2.service_account import Credentials
import gspread
import requests
import os
import re
import uuid
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

GREETING_TEXT     = "שלום! רוצה לקבל פרטים בוואטסאפ? תגיד כן."
SUCCESS_TEXT      = "מעולה! שלחתי לך עכשיו בוואטסאפ. תודה!"
RETRY_TEXT        = "לא קלטתי. תגיד כן אם תרצה פרטים בוואטסאפ."
FINAL_TEXT        = "תודה ולהתראות."
NO_INTEREST_TEXT  = "בסדר, תודה על השיחה. להתראות!"

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
    if not sheet:
        return
    try:
        if sheet.row_values(1) != EXPECTED_HEADERS:
            sheet.update("A1:N1", [EXPECTED_HEADERS])
            print("✅ Headers fixed", flush=True)
    except Exception as e:
        print("🔥 HEADER FIX:", e, flush=True)

def append_lead_row(row):
    if not sheet:
        return
    ensure_headers()
    try:
        sheet.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print("🔥 SHEET APPEND:", e, flush=True)

def get_sheet_rows():
    if not sheet:
        return []
    ensure_headers()
    try:
        return sheet.get_all_records(expected_headers=EXPECTED_HEADERS)
    except Exception as e:
        print("🔥 GET ROWS:", e, flush=True)
        return []

def normalize_phone(phone: str) -> str:
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

# =========================
# Grok TTS with cache
# =========================
_tts_cache = {}

def grok_tts(text: str) -> str:
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
            "text": text[:1000],
            "voice_id": GROK_TTS_VOICE,
            "language": GROK_TTS_LANGUAGE,
            "format": "mp3",
        },
        timeout=15,
    )

    print(f"🔊 TTS {res.status_code} {len(res.content)}b", flush=True)

    if res.status_code != 200:
        raise RuntimeError(f"TTS {res.status_code}: {res.text[:150]}")

    fname = f"{uuid.uuid4()}.mp3"
    path = os.path.join(AUDIO_DIR, fname)

    with open(path, "wb") as f:
        f.write(res.content)

    url = f"{PUBLIC_BASE_URL}/audio/{fname}"
    _tts_cache[text] = url
    return url

def prewarm_tts():
    if not XAI_API_KEY:
        return

    for text in [GREETING_TEXT, SUCCESS_TEXT, RETRY_TEXT, FINAL_TEXT, NO_INTEREST_TEXT]:
        try:
            grok_tts(text)
            print(f"🎵 Pre-warmed: {text[:40]}", flush=True)
        except Exception as e:
            print(f"⚠️ Prewarm failed: {e}", flush=True)

def play_or_say(target, text: str):
    try:
        target.play(grok_tts(text))
    except Exception as e:
        print(f"⚠️ TTS fallback: {e}", flush=True)
        target.say(text, language="he-IL")

@app.get("/audio/<fname>")
def serve_audio(fname):
    path = os.path.join(AUDIO_DIR, fname)

    if not os.path.exists(path):
        return Response("not found", status=404)

    with open(path, "rb") as f:
        data = f.read()

    return Response(
        data,
        mimetype="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )

# =========================
# WhatsApp
# =========================
def send_whatsapp(to_phone: str, summary: str = "") -> str:
    if not twilio_client:
        raise RuntimeError("Twilio not initialized")

    from_num = TWILIO_WHATSAPP_FROM

    if not from_num:
        raise RuntimeError(
            "TWILIO_WHATSAPP_FROM missing. Use whatsapp:+14155238886 for sandbox or approved sender."
        )

    if not from_num.startswith("whatsapp:"):
        from_num = f"whatsapp:{from_num}"

    to_num = normalize_phone(to_phone)

    print(f"📱 WA: {from_num} → {to_num}", flush=True)

    msg = twilio_client.messages.create(
        from_=from_num,
        to=to_num,
        body=(
            "היי, תודה על השיחה 🙏\n\n"
            "כמו שביקשת, הנה הפרטים:\n"
            f"{summary}\n\n"
            "נשמח לעזור ולתאם המשך!"
        ),
    )

    return msg.sid

# =========================
# Interest Detection
# =========================
YES_WORDS = [
    "כן", "בטח", "ברור", "אפשר", "יאללה", "יאלה", "סבבה",
    "אוקיי", "אוקי", "שלח", "תשלח", "תשלחי", "שלחו",
    "וואטסאפ", "ווטסאפ", "ווצאפ", "מעוניין", "מעוניינת",
    "רוצה", "אשמח", "פרטים", "יכול", "נשמח", "קדימה",
    "yes", "yeah", "yep", "ok", "okay", "sure", "please",
    "send", "good", "great", "ken", "tov",
]

NO_WORDS = [
    "לא מעוניין", "לא מעוניינת", "לא רלוונטי", "לא תודה",
    "אל תשלח", "לא לשלוח", "תוריד", "עזוב",
    "no thanks", "not interested", "not now",
]

def detect_interest(text: str) -> bool:
    if not text:
        return False

    clean = re.sub(r"[^\w\u0590-\u05FF\s]", " ", text.lower().strip())
    clean = re.sub(r"\s+", " ", clean).strip()

    print(f"🔍 DETECT: '{clean}'", flush=True)

    for phrase in NO_WORDS:
        if phrase in clean:
            print(f"❌ NO: {phrase}", flush=True)
            return False

    if re.search(r"(?<!\w)לא(?!\w)", clean):
        strong_yes = ["כן", "בטח", "ברור", "אשמח", "רוצה", "yes", "ok", "okay", "sure", "good"]
        if not any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", clean) for w in strong_yes):
            print("❌ Standalone לא", flush=True)
            return False

    for word in YES_WORDS:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", clean):
            print(f"✅ YES: {word}", flush=True)
            return True

    print("⚪ No match", flush=True)
    return False

# =========================
# Voice — no recording
# =========================
@app.post("/voice")
def voice():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")

    print(f"📞 Incoming: {caller} | {call_sid}", flush=True)

    r = VoiceResponse()

    gather = r.gather(
        input="speech",
        action="/gather",
        method="POST",
        language="he-IL",
        speech_timeout=2,
        timeout=12,
        hints=(
            "כן,בטח,ברור,סבבה,שלח,תשלח,וואטסאפ,אשמח,רוצה,"
            "yes,yeah,ok,okay,sure,good"
        ),
    )

    play_or_say(gather, GREETING_TEXT)

    r.redirect("/no-input")

    return Response(str(r), mimetype="text/xml")

# =========================
# Gather
# =========================
@app.post("/gather")
def gather():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")
    speech_result = request.form.get("SpeechResult", "")

    print(f"🎤 SPEECH: {repr(speech_result)} | {caller}", flush=True)

    interested = detect_interest(speech_result)

    wa_sent = "no"
    wa_sid = ""
    summary = "הלקוח ביקש לקבל פרטים בוואטסאפ." if interested else "לא זוהה עניין."

    if interested and caller:
        try:
            wa_sid = send_whatsapp(caller, "נשמח להמשך שיחה ותיאום.")
            wa_sent = "yes"
            print(f"✅ WA sent → {caller} | {wa_sid}", flush=True)
        except Exception as e:
            wa_sent = f"failed:{str(e)[:120]}"
            print(f"🔥 WA ERROR: {e}", flush=True)

    row = [
        now_iso(),
        call_sid,
        caller,
        speech_result,
        "yes" if interested else "no",
        "positive" if interested else "neutral",
        "hot" if interested else "cold",
        "send_whatsapp" if interested else "no_action",
        summary,
        wa_sent,
        wa_sid,
        "",
        "",
        "",
    ]

    threading.Thread(target=append_lead_row, args=(row,), daemon=True).start()

    r = VoiceResponse()

    if interested:
        if wa_sent == "yes":
            play_or_say(r, SUCCESS_TEXT)
        else:
            play_or_say(
                r,
                "מעולה, קיבלתי את האישור שלך. הייתה בעיה בשליחת הוואטסאפ, אבל שמרתי את הפרטים ונחזור אליך.",
            )
        r.hangup()
        return Response(str(r), mimetype="text/xml")

    gather2 = r.gather(
        input="speech",
        action="/gather-final",
        method="POST",
        language="he-IL",
        speech_timeout=2,
        timeout=8,
        hints="כן,בטח,ברור,שלח,וואטסאפ,yes,ok,sure,good",
    )

    play_or_say(gather2, RETRY_TEXT)
    play_or_say(r, NO_INTEREST_TEXT)
    r.hangup()

    return Response(str(r), mimetype="text/xml")

# =========================
# Gather Final
# =========================
@app.post("/gather-final")
def gather_final():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")
    speech_result = request.form.get("SpeechResult", "")

    print(f"🎤 FINAL: {repr(speech_result)} | {caller}", flush=True)

    interested = detect_interest(speech_result)

    wa_sent = "no"
    wa_sid = ""

    if interested and caller:
        try:
            wa_sid = send_whatsapp(caller, "נשמח להמשך שיחה ותיאום.")
            wa_sent = "yes"
            print(f"✅ WA sent final → {caller} | {wa_sid}", flush=True)
        except Exception as e:
            wa_sent = f"failed:{str(e)[:120]}"
            print(f"🔥 WA ERROR FINAL: {e}", flush=True)

    row = [
        now_iso(),
        call_sid + "_retry",
        caller,
        speech_result,
        "yes" if interested else "no",
        "positive" if interested else "neutral",
        "hot" if interested else "cold",
        "send_whatsapp" if interested else "no_action",
        "ניסיון שני - מעוניין" if interested else "ניסיון שני - לא מעוניין",
        wa_sent,
        wa_sid,
        "",
        "",
        "",
    ]

    threading.Thread(target=append_lead_row, args=(row,), daemon=True).start()

    r = VoiceResponse()

    if interested:
        if wa_sent == "yes":
            play_or_say(r, SUCCESS_TEXT)
        else:
            play_or_say(
                r,
                "מעולה, קיבלתי את האישור שלך. הייתה בעיה בשליחת הוואטסאפ, אבל שמרתי את הפרטים ונחזור אליך.",
            )
    else:
        play_or_say(r, FINAL_TEXT)

    r.hangup()

    return Response(str(r), mimetype="text/xml")

# =========================
# No Input
# =========================
@app.post("/no-input")
def no_input():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")

    print(f"🔇 No input | {caller}", flush=True)

    row = [
        now_iso(),
        call_sid,
        caller,
        "",
        "no",
        "neutral",
        "cold",
        "no_action",
        "לא ענה",
        "no",
        "",
        "",
        "",
        "",
    ]

    threading.Thread(target=append_lead_row, args=(row,), daemon=True).start()

    r = VoiceResponse()
    play_or_say(r, FINAL_TEXT)
    r.hangup()

    return Response(str(r), mimetype="text/xml")

# =========================
# Tests
# =========================
@app.get("/test-whatsapp")
def test_whatsapp():
    to = request.args.get("to", "").strip()

    if not to:
        return jsonify({
            "ok": False,
            "error": "missing ?to=+972...",
            "twilio_whatsapp_from": TWILIO_WHATSAPP_FROM or "NOT SET",
        })

    try:
        sid = send_whatsapp(to, "זו הודעת בדיקה.")
        return jsonify({"ok": True, "sid": sid})
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "from": TWILIO_WHATSAPP_FROM,
            "tip": "For sandbox use TWILIO_WHATSAPP_FROM=whatsapp:+14155238886",
        }), 500

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

    total = len(rows)
    interested = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")
    wa_sent = sum(1 for r in rows if str(r.get("whatsapp_sent", "")).lower().startswith("yes"))

    return jsonify({
        "ok": True,
        "total_calls": total,
        "interested": interested,
        "whatsapp_sent": wa_sent,
        "conversion_pct": round(interested / total * 100, 2) if total else 0,
    })

@app.get("/dashboard")
def dashboard():
    rows = get_sheet_rows()

    total = len(rows)
    interested = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")
    wa_sent = sum(1 for r in rows if str(r.get("whatsapp_sent", "")).lower().startswith("yes"))
    conversion = round(interested / total * 100, 2) if total else 0
    recent = list(reversed(rows[-30:]))

    table_rows = ""

    for r in recent:
        ib = "🟢 כן" if str(r.get("interested", "")).lower() == "yes" else "🔴 לא"
        wb = "🟢 נשלח" if str(r.get("whatsapp_sent", "")).lower().startswith("yes") else "⚪ לא"

        table_rows += (
            f"<tr>"
            f"<td>{r.get('timestamp', '')}</td>"
            f"<td>{r.get('caller', '')}</td>"
            f"<td>{ib}</td>"
            f"<td>{wb}</td>"
            f"<td>{r.get('lead_quality', '')}</td>"
            f"<td>{r.get('sentiment', '')}</td>"
            f"<td>{r.get('summary', '')}</td>"
            f"<td>{r.get('user_text', '')}</td>"
            f"</tr>"
        )

    html = f"""
<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<title>AI Calls Dashboard</title>
<style>
body {{
    font-family: Arial;
    background: #f7f7f7;
    padding: 24px;
}}
.cards {{
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 24px;
}}
.card {{
    background: white;
    padding: 18px;
    border-radius: 12px;
    min-width: 180px;
    box-shadow: 0 2px 8px rgba(0,0,0,.08);
}}
.num {{
    font-size: 30px;
    font-weight: bold;
    color: #333;
}}
table {{
    width: 100%;
    border-collapse: collapse;
    background: white;
    border-radius: 8px;
    overflow: hidden;
}}
th, td {{
    padding: 10px 12px;
    border-bottom: 1px solid #eee;
    vertical-align: top;
    font-size: 13px;
}}
th {{
    background: #111;
    color: white;
}}
tr:hover {{
    background: #f9f9f9;
}}
</style>
</head>
<body>
<h1>📊 דשבורד שיחות AI</h1>

<div class="cards">
    <div class="card"><div>סה״כ שיחות</div><div class="num">{total}</div></div>
    <div class="card"><div>מתעניינים</div><div class="num">{interested}</div></div>
    <div class="card"><div>וואטסאפ נשלח</div><div class="num">{wa_sent}</div></div>
    <div class="card"><div>המרה</div><div class="num">{conversion}%</div></div>
</div>

<table>
<tr>
    <th>זמן</th>
    <th>טלפון</th>
    <th>עניין</th>
    <th>וואטסאפ</th>
    <th>איכות</th>
    <th>סנטימנט</th>
    <th>סיכום</th>
    <th>מה אמר</th>
</tr>
{table_rows or '<tr><td colspan="8" style="text-align:center;padding:30px;color:#999">אין שיחות עדיין</td></tr>'}
</table>
</body>
</html>
"""

    return Response(html, mimetype="text/html; charset=utf-8")

# =========================
# Health
# =========================
@app.get("/")
def home():
    ensure_headers()

    return jsonify({
        "ok": True,
        "service": "AI sales call bot v4 - no recording",
        "flow": "Twilio Gather fast → detect interest → WhatsApp → Sheets",
        "recording": "disabled",
        "whatsapp_from": TWILIO_WHATSAPP_FROM or "NOT SET",
        "tts_voice": GROK_TTS_VOICE,
        "tts_language": GROK_TTS_LANGUAGE,
        "routes": [
            "/voice",
            "/dashboard",
            "/stats",
            "/test-whatsapp?to=+972...",
            "/test-detect?text=כן",
            "/test-tts?text=שלום",
        ],
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

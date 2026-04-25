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
from datetime import datetime

app = Flask(__name__)

# =========================
# ENV
# =========================
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_CALLER_ID = os.getenv("TWILIO_CALLER_ID")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")

XAI_API_KEY = os.getenv("XAI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")

AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# =========================
# Twilio Client
# =========================
twilio_client = None
try:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    print("✅ Twilio connected", flush=True)
except Exception as e:
    print("🔥 TWILIO ERROR:", str(e), flush=True)

# =========================
# Google Sheets
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

google_service_account_info = {
    "type": os.getenv("GOOGLE_TYPE"),
    "project_id": os.getenv("GOOGLE_PROJECT_ID"),
    "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_ID"),
    "private_key": os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n"),
    "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
    "client_id": os.getenv("GOOGLE_CLIENT_ID"),
    "auth_uri": os.getenv("GOOGLE_AUTH_URI"),
    "token_uri": os.getenv("GOOGLE_TOKEN_URI"),
    "auth_provider_x509_cert_url": os.getenv("GOOGLE_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_X509_CERT_URL"),
    "universe_domain": os.getenv("GOOGLE_UNIVERSE_DOMAIN", "googleapis.com"),
}

sheet = None
try:
    creds = Credentials.from_service_account_info(
        google_service_account_info,
        scopes=SCOPES,
    )
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open_by_key(GOOGLE_SHEETS_ID).sheet1
    print("✅ Google Sheets connected", flush=True)
except Exception as e:
    print("🔥 GOOGLE SHEETS ERROR:", str(e), flush=True)

# =========================
# Helpers
# =========================
def now_iso():
    return datetime.utcnow().isoformat()

def normalize_phone_for_whatsapp(phone: str) -> str:
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

def append_lead_row(row: list):
    if sheet is None:
        print("⚠️ Google Sheet not connected", flush=True)
        return False

    sheet.append_row(row, value_input_option="USER_ENTERED")
    return True

def safe_json_loads(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return {}

# =========================
# WhatsApp
# =========================
def send_whatsapp(to_phone: str, business_name: str = "", summary: str = ""):
    if twilio_client is None:
        raise RuntimeError("Twilio client not initialized")

    if not TWILIO_WHATSAPP_FROM:
        raise RuntimeError("TWILIO_WHATSAPP_FROM missing")

    to_whatsapp = normalize_phone_for_whatsapp(to_phone)

    body = (
        f"היי{(' ' + business_name) if business_name else ''}, תודה על השיחה 🙏\n\n"
        f"כמו שביקשת, הנה הפרטים להמשך:\n"
        f"{summary}\n\n"
        f"נשמח לעזור ולתאם המשך."
    )

    msg = twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=to_whatsapp,
        body=body,
    )

    return msg.sid

# =========================
# Grok / xAI STT
# =========================
def grok_stt(recording_url: str) -> str:
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY missing")

    audio_res = requests.get(
        recording_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=20,
    )

    if audio_res.status_code != 200:
        raise RuntimeError(f"Could not download recording: {audio_res.status_code}")

    files = {
        "file": ("recording.wav", audio_res.content, "audio/wav")
    }

    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
    }

    res = requests.post(
        "https://api.x.ai/v1/stt",
        headers=headers,
        files=files,
        timeout=40,
    )

    if res.status_code != 200:
        raise RuntimeError(f"Grok STT error {res.status_code}: {res.text}")

    data = res.json()
    return data.get("text", "").strip()

# =========================
# OpenAI Thinking + Lead Analysis
# =========================
SYSTEM_PROMPT = """
אתה נציג מכירות טלפוני בעברית לעסקים קטנים.

המטרה:
1. להבין אם הלקוח מעוניין.
2. לענות קצר, טבעי וברור.
3. אם הלקוח אומר כן / מעוניין / תשלח לי / שלח וואטסאפ / דבר איתי / אשמח / רוצה פרטים — סמן interested=true.
4. אם הלקוח מסרב, עסוק, לא רלוונטי, או מבקש לא לפנות — סמן interested=false.
5. אם יש עניין, כתוב תשובת קול קצרה שאומרת שנשלח לו וואטסאפ.
6. אל תישמע כמו רובוט.
7. אל תכתוב יותר מ־2 משפטים בתשובת הקול.

חובה להחזיר JSON בלבד בפורמט הבא:
{
  "reply": "תשובה קולית קצרה ללקוח",
  "interested": true,
  "sentiment": "positive / neutral / negative",
  "summary": "סיכום קצר של השיחה",
  "next_action": "send_whatsapp / continue_call / no_action",
  "lead_quality": "hot / warm / cold"
}
"""

def openai_analyze(user_text: str, caller: str = "") -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": f"""
מספר לקוח: {caller}

הלקוח אמר:
{user_text}
""",
            },
        ],
    }

    res = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=40,
    )

    if res.status_code != 200:
        raise RuntimeError(f"OpenAI error {res.status_code}: {res.text}")

    data = res.json()
    text = data.get("output_text", "").strip()
    parsed = safe_json_loads(text)

    return {
        "reply": parsed.get("reply", "תודה, אני שולח לך פרטים בוואטסאפ."),
        "interested": bool(parsed.get("interested", False)),
        "sentiment": parsed.get("sentiment", "neutral"),
        "summary": parsed.get("summary", user_text[:200]),
        "next_action": parsed.get("next_action", "continue_call"),
        "lead_quality": parsed.get("lead_quality", "cold"),
        "raw": text,
    }

# =========================
# Grok / xAI TTS - Leo
# =========================
def grok_tts(text: str) -> str:
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY missing")

    payload = {
        "text": text[:1500],
        "voice_id": "leo",
        "language": "auto",
        "format": "mp3",
    }

    res = requests.post(
        "https://api.x.ai/v1/tts",
        headers={
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=40,
    )

    if res.status_code != 200:
        raise RuntimeError(f"Grok TTS error {res.status_code}: {res.text}")

    file_id = str(uuid.uuid4())
    path = os.path.join(AUDIO_DIR, f"{file_id}.mp3")

    with open(path, "wb") as f:
        f.write(res.content)

    return f"{PUBLIC_BASE_URL}/audio/{file_id}.mp3"

# =========================
# Audio route
# =========================
@app.get("/audio/<file_name>")
def serve_audio(file_name):
    path = os.path.join(AUDIO_DIR, file_name)

    if not os.path.exists(path):
        return Response("not found", status=404)

    with open(path, "rb") as f:
        audio = f.read()

    return Response(audio, mimetype="audio/mpeg")

# =========================
# Twilio Voice Entry
# =========================
@app.post("/voice")
def voice():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")

    print(f"📞 Incoming call from {caller} | {call_sid}", flush=True)

    r = VoiceResponse()

    r.say(
        "שלום, מדבר עוזר דיגיטלי שעוזר לעסקים לא לפספס לקוחות. תרצה שאשלח לך פרטים בוואטסאפ?",
        language="he-IL",
        voice="alice",
    )

    r.record(
        action="/process",
        method="POST",
        max_length=12,
        timeout=4,
        play_beep=True,
        transcribe=False,
    )

    r.say("לא התקבלה תשובה. תודה ולהתראות.", language="he-IL", voice="alice")
    r.hangup()

    return Response(str(r), mimetype="text/xml")

# =========================
# Process recording
# =========================
@app.post("/process")
def process():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")
    recording_url = request.form.get("RecordingUrl", "")

    user_text = ""
    analysis = {}
    whatsapp_sent = "no"
    whatsapp_sid = ""
    tts_error = ""
    stt_error = ""
    openai_error = ""

    try:
        if recording_url:
            user_text = grok_stt(recording_url + ".wav")
        else:
            user_text = ""
    except Exception as e:
        stt_error = str(e)
        print("🔥 STT ERROR:", stt_error, flush=True)

    try:
        analysis = openai_analyze(user_text, caller=caller)
    except Exception as e:
        openai_error = str(e)
        print("🔥 OPENAI ERROR:", openai_error, flush=True)
        analysis = {
            "reply": "תודה, אני שולח לך פרטים בוואטסאפ.",
            "interested": False,
            "sentiment": "neutral",
            "summary": "OpenAI failed",
            "next_action": "no_action",
            "lead_quality": "cold",
        }

    interested = analysis.get("interested", False)
    reply = analysis.get("reply", "תודה רבה.")
    summary = analysis.get("summary", "")

    if interested and caller:
        try:
            whatsapp_sid = send_whatsapp(
                to_phone=caller,
                summary=summary or "נשמח להמשך שיחה ותיאום.",
            )
            whatsapp_sent = "yes"
        except Exception as e:
            whatsapp_sent = f"failed: {str(e)[:120]}"
            print("🔥 WHATSAPP ERROR:", str(e), flush=True)

    row = [
        now_iso(),
        call_sid,
        caller,
        user_text,
        "yes" if interested else "no",
        analysis.get("sentiment", ""),
        analysis.get("lead_quality", ""),
        analysis.get("next_action", ""),
        summary,
        whatsapp_sent,
        whatsapp_sid,
        stt_error,
        openai_error,
        tts_error,
    ]

    try:
        append_lead_row(row)
    except Exception as e:
        print("🔥 SHEET APPEND ERROR:", str(e), flush=True)

    r = VoiceResponse()

    try:
        audio_url = grok_tts(reply)
        r.play(audio_url)
    except Exception as e:
        tts_error = str(e)
        print("🔥 TTS ERROR:", tts_error, flush=True)
        r.say(reply, language="he-IL", voice="alice")

    if interested:
        r.say("שלחתי לך הודעה. תודה רבה ולהתראות.", language="he-IL", voice="alice")
        r.hangup()
    else:
        r.record(
            action="/process",
            method="POST",
            max_length=12,
            timeout=4,
            play_beep=True,
            transcribe=False,
        )
        r.say("תודה רבה ולהתראות.", language="he-IL", voice="alice")
        r.hangup()

    return Response(str(r), mimetype="text/xml")

# =========================
# Manual test WhatsApp
# =========================
@app.get("/test-whatsapp")
def test_whatsapp():
    to = request.args.get("to", "")

    if not to:
        return jsonify({"ok": False, "error": "missing ?to=+972..."})

    try:
        sid = send_whatsapp(
            to_phone=to,
            summary="זו הודעת בדיקה מהמערכת.",
        )
        return jsonify({"ok": True, "sid": sid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# Stats API
# =========================
@app.get("/stats")
def stats():
    if sheet is None:
        return jsonify({"ok": False, "error": "Google Sheets not connected"}), 500

    rows = sheet.get_all_records()

    total = len(rows)
    interested = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")
    whatsapp_sent = sum(1 for r in rows if str(r.get("whatsapp_sent", "")).lower().startswith("yes"))

    hot = sum(1 for r in rows if str(r.get("lead_quality", "")).lower() == "hot")
    warm = sum(1 for r in rows if str(r.get("lead_quality", "")).lower() == "warm")
    cold = sum(1 for r in rows if str(r.get("lead_quality", "")).lower() == "cold")

    return jsonify({
        "ok": True,
        "total_calls": total,
        "interested": interested,
        "whatsapp_sent": whatsapp_sent,
        "conversion_pct": round((interested / total) * 100, 2) if total else 0,
        "whatsapp_send_pct": round((whatsapp_sent / interested) * 100, 2) if interested else 0,
        "lead_quality": {
            "hot": hot,
            "warm": warm,
            "cold": cold,
        }
    })

# =========================
# Dashboard
# =========================
@app.get("/dashboard")
def dashboard():
    if sheet is None:
        return Response(
            "<h1>Google Sheets not connected</h1>",
            mimetype="text/html; charset=utf-8",
            status=500,
        )

    rows = sheet.get_all_records()

    total = len(rows)
    interested = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")
    whatsapp_sent = sum(1 for r in rows if str(r.get("whatsapp_sent", "")).lower().startswith("yes"))

    conversion = round((interested / total) * 100, 2) if total else 0
    wa_rate = round((whatsapp_sent / interested) * 100, 2) if interested else 0

    recent_rows = list(reversed(rows[-30:]))

    table_rows = ""
    for r in recent_rows:
        interested_badge = "🟢 כן" if str(r.get("interested", "")).lower() == "yes" else "🔴 לא"
        wa_badge = "🟢 נשלח" if str(r.get("whatsapp_sent", "")).lower().startswith("yes") else "⚪ לא"
        quality = r.get("lead_quality", "")

        table_rows += f"""
        <tr>
            <td>{r.get("timestamp", "")}</td>
            <td>{r.get("caller", "")}</td>
            <td>{interested_badge}</td>
            <td>{wa_badge}</td>
            <td>{quality}</td>
            <td>{r.get("sentiment", "")}</td>
            <td style="max-width:360px;white-space:normal;">{r.get("summary", "")}</td>
            <td style="max-width:360px;white-space:normal;">{r.get("user_text", "")}</td>
        </tr>
        """

    html = f"""
<!doctype html>
<html lang="he" dir="rtl">
<head>
    <meta charset="utf-8">
    <title>AI Calls Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style>
        body {{
            font-family: Arial, sans-serif;
            background: #f7f7f7;
            padding: 24px;
            color: #222;
        }}
        .cards {{
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
            margin-bottom: 24px;
        }}
        .card {{
            background: white;
            border-radius: 12px;
            padding: 18px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            min-width: 180px;
        }}
        .num {{
            font-size: 30px;
            font-weight: bold;
            margin-top: 6px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 12px;
            overflow: hidden;
        }}
        th, td {{
            border-bottom: 1px solid #eee;
            padding: 10px;
            vertical-align: top;
        }}
        th {{
            background: #111;
            color: white;
        }}
    </style>
</head>
<body>
    <h1>דשבורד שיחות AI</h1>

    <div class="cards">
        <div class="card">
            <div>סה״כ שיחות</div>
            <div class="num">{total}</div>
        </div>
        <div class="card">
            <div>מתעניינים</div>
            <div class="num">{interested}</div>
        </div>
        <div class="card">
            <div>וואטסאפ נשלח</div>
            <div class="num">{whatsapp_sent}</div>
        </div>
        <div class="card">
            <div>המרה</div>
            <div class="num">{conversion}%</div>
        </div>
        <div class="card">
            <div>שליחת וואטסאפ מתוך מתעניינים</div>
            <div class="num">{wa_rate}%</div>
        </div>
    </div>

    <h2>30 השיחות האחרונות</h2>

    <table>
        <tr>
            <th>זמן</th>
            <th>טלפון</th>
            <th>עניין</th>
            <th>וואטסאפ</th>
            <th>איכות ליד</th>
            <th>סנטימנט</th>
            <th>סיכום</th>
            <th>מה הלקוח אמר</th>
        </tr>
        {table_rows if table_rows else '<tr><td colspan="8">אין שיחות עדיין</td></tr>'}
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
    return jsonify({
        "ok": True,
        "service": "AI sales call bot",
        "routes": ["/voice", "/dashboard", "/stats", "/test-whatsapp?to=+972..."]
    })

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

# =========================
# Run local
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

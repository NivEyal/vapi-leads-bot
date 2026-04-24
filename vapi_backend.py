from flask import Flask, request, jsonify, Response, send_file
from datetime import datetime
import os
import re
import io
import requests

from twilio.rest import Client
import gspread
from google.oauth2.service_account import Credentials

# =========================
# App
# =========================
app = Flask(__name__)

# =========================
# ENV
# =========================
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM  = os.getenv("TWILIO_WHATSAPP_FROM")
VAPI_WEBHOOK_SECRET   = os.getenv("VAPI_WEBHOOK_SECRET")
GOOGLE_SHEETS_ID      = os.getenv("GOOGLE_SHEETS_ID")
AZURE_TTS_KEY         = os.getenv("AZURE_TTS_KEY")
AZURE_TTS_REGION      = os.getenv("AZURE_TTS_REGION", "westeurope")

# =========================
# Google Service Account
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

google_service_account_info = {
    "type":                        os.getenv("GOOGLE_TYPE"),
    "project_id":                  os.getenv("GOOGLE_PROJECT_ID"),
    "private_key_id":              os.getenv("GOOGLE_PRIVATE_KEY_I"),
    "private_key":                 os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n"),
    "client_email":                os.getenv("GOOGLE_CLIENT_EMAIL"),
    "client_id":                   os.getenv("GOOGLE_CLIENT_ID"),
    "auth_uri":                    os.getenv("GOOGLE_AUTH_URI"),
    "token_uri":                   os.getenv("GOOGLE_TOKEN_URI"),
    "auth_provider_x509_cert_url": os.getenv("GOOGLE_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url":        os.getenv("GOOGLE_CLIENT_X509_CERT_URL"),
    "universe_domain":             os.getenv("GOOGLE_UNIVERSE_DOMAIN", "googleapis.com"),
}

# =========================
# Google Sheets client
# =========================
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
    sheet = None

# =========================
# Twilio client
# =========================
try:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
except Exception as e:
    print("🔥 TWILIO ERROR:", str(e), flush=True)
    twilio_client = None

# =========================
# Helpers
# =========================
INTEREST_KEYWORDS = [
    "כן", "מעוניין", "תשלח", "שלח לי", "תשלחו",
    "וואטסאפ", "פרטים", "אשמח", "אשמח לקבל", "דבר איתי",
]

def is_interested(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k.lower() in t for k in INTEREST_KEYWORDS)

def normalize_phone_for_whatsapp(phone: str) -> str:
    if not phone:
        return ""
    if phone.startswith("whatsapp:"):
        return phone
    digits = re.sub(r"[^\d+]", "", phone)
    if digits.startswith("0"):
        digits = "+972" + digits[1:]
    elif not digits.startswith("+"):
        digits = "+" + digits
    return f"whatsapp:{digits}"

def append_lead_row(row: list):
    if sheet is None:
        print("⚠️ Sheet is not connected. Row not saved.", flush=True)
        return False
    sheet.append_row(row, value_input_option="USER_ENTERED")
    return True

def send_whatsapp(to_phone: str, business_name: str = "", summary: str = ""):
    if twilio_client is None:
        raise RuntimeError("Twilio client not initialized")
    to_whatsapp = normalize_phone_for_whatsapp(to_phone)
    body = (
        f"היי{(' ' + business_name) if business_name else ''}, תודה על השיחה.\n"
        f"כמו שביקשת, הנה הפרטים להמשך.\n"
        f"{summary}"
    )
    msg = twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=to_whatsapp,
        body=body,
    )
    return msg.sid

def is_authorized(req) -> bool:
    auth_header = req.headers.get("Authorization", "")
    expected = f"Bearer {VAPI_WEBHOOK_SECRET}"
    return auth_header == expected
def fix_hebrew_tts(text: str) -> str:
    # ניקוד חכם למילים בעייתיות בלבד (לא הכל!)
    replacements = {
        "שלום": "שָׁלוֹם",
        "היי": "הַיי",
        "מדבר": "מְדַבֵּר",
        "ניב": "נִיב",
        "תודה": "תוֹדָה",
        "השיחה": "הַשִּׂיחָה",
        "כמו שביקשת": "כְּמוֹ שֶׁבִּיקַּשְׁתָּ",
        "הנה הפרטים": "הִנֵּה הַפְּרָטִים",
        "להמשך": "לְהֶמְשֵׁךְ",
        "תור": "תוֹר",
        "לקביעת תור": "לִקְבִּיעַת תוֹר",
        "שלחתי": "שָׁלַחְתִּי",
        "לך": "לְךָ",
        "וואטסאפ": "וָואטְסְאַפּ",
        "נשמח": "נִשְׂמַח",
        "להמשך שיחה": "לְהֶמְשֵׁךְ שִׂיחָה",
        "ותיאום": "וְתִיאוּם",
    }

    for k, v in replacements.items():
        text = text.replace(k, v)

    return text
def azure_tts(text: str, voice: str = "he-IL-AvriNeural", output_format: str = "raw-16khz-16bit-mono-pcm") -> bytes:
    url = f"https://{AZURE_TTS_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"

    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_TTS_KEY,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": output_format,
        "User-Agent": "vapi-tts",
    }

    # 👉 ניקוד חכם
    text = fix_hebrew_tts(text)

    # 👉 SSML מקצועי (זה מה שמשפר באמת)
    ssml = f"""
    <speak version='1.0' xml:lang='he-IL'>
        <voice name='{voice}'>
            <prosody rate="0.95" pitch="+0%">
                {text}
            </prosody>
        </voice>
    </speak>
    """

    res = requests.post(url, headers=headers, data=ssml.encode("utf-8"), timeout=8)

    if res.status_code != 200:
        raise RuntimeError(f"Azure TTS error {res.status_code}: {res.text}")

    return res.content
# =========================
# Health
# =========================
@app.get("/")
def home():
    return {"status": "server running", "message": "Vapi backend is live"}, 200

@app.get("/healthz")
def healthz():
    return {"ok": True}, 200

# =========================
# Azure TTS test - browser
# =========================
@app.get("/test-tts")
def test_tts():
    if not AZURE_TTS_KEY:
        return {"ok": False, "error": "AZURE_TTS_KEY not set"}, 500
    try:
        audio = azure_tts(
            text="שלום, מדבר ניב. זה טסט למערכת.",
            voice="he-IL-AvriNeural",
            output_format="audio-16khz-32kbitrate-mono-mp3",
        )
        return send_file(
            io.BytesIO(audio),
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name="speech.mp3",
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# =========================
# Custom TTS endpoint for Vapi
# =========================
@app.post("/vapi-tts")
def vapi_tts():
    if not AZURE_TTS_KEY:
        return Response(b"", mimetype="application/octet-stream")

    data = request.get_json(silent=True) or {}
    print("🔥 VAPI TTS REQUEST:", data, flush=True)

    message = data.get("message", {}) or {}
    text = message.get("text") or data.get("text") or ""

    if not text:
        return Response(b"", mimetype="application/octet-stream")

    text = text[:300]

    voice = (
        data.get("voiceId")
        or message.get("voiceId")
        or "he-IL-AvriNeural"
    )

    try:
        audio = azure_tts(text=text, voice=voice, output_format="raw-16khz-16bit-mono-pcm")
        return Response(audio, mimetype="application/octet-stream")
    except Exception as e:
        print("🔥 AZURE TTS ERROR:", str(e), flush=True)
        return Response(b"", mimetype="application/octet-stream")

# =========================
# Webhook from Vapi
# =========================
@app.post("/webhooks/vapi")
def vapi_webhook():
    if not is_authorized(request):
        print("❌ Unauthorized webhook", flush=True)
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    message = data.get("message", {})
    msg_type = message.get("type")

    print("🔥 MESSAGE TYPE:", msg_type, flush=True)

    if msg_type != "end-of-call-report":
        return ("", 204)

    call         = message.get("call", {}) or {}
    artifact     = message.get("artifact", {}) or {}
    call_id      = call.get("id", "")
    phone_number = (
        call.get("customer", {}).get("number")
        or call.get("phoneNumber")
        or ""
    )
    business_name = call.get("customer", {}).get("name", "") or ""
    ended_reason  = message.get("endedReason", "")
    transcript    = artifact.get("transcript") or call.get("transcript") or ""

    interested = is_interested(transcript)
    answered   = ended_reason not in [
        "customer-did-not-answer",
        "assistant-not-available",
        "assistant-request-returned-no-assistant",
    ]

    summary = (
        "הלקוח הביע עניין וביקש פרטים בוואטסאפ."
        if interested
        else "לא זוהה עניין ברור."
    )

    whatsapp_sent = "no"
    whatsapp_sid  = ""

    if interested and phone_number:
        try:
            whatsapp_sid  = send_whatsapp(
                to_phone=phone_number,
                business_name=business_name,
                summary="נשמח להמשך שיחה ותיאום.",
            )
            whatsapp_sent = "yes"
        except Exception as e:
            whatsapp_sent = f"failed: {str(e)[:120]}"
            print("🔥 WHATSAPP ERROR:", str(e), flush=True)

    row = [
        datetime.utcnow().isoformat(),
        call_id,
        phone_number,
        business_name,
        "yes" if answered else "no",
        "yes" if interested else "no",
        summary,
        transcript[:4000],
        whatsapp_sent,
        whatsapp_sid,
        ended_reason,
    ]

    append_lead_row(row)
    return ("", 204)

# =========================
# Stats
# =========================
@app.get("/stats")
def stats():
    if sheet is None:
        return jsonify({"ok": False, "error": "Google Sheets not connected"}), 500

    rows = sheet.get_all_records()
    total_calls     = len(rows)
    answered_calls  = sum(1 for r in rows if str(r.get("answered", "")).lower() == "yes")
    interested_calls = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")

    return jsonify({
        "total_calls":                    total_calls,
        "answered_calls":                 answered_calls,
        "interested_calls":               interested_calls,
        "interest_rate_from_total_pct":   round((interested_calls / total_calls) * 100, 2) if total_calls else 0,
        "interest_rate_from_answered_pct": round((interested_calls / answered_calls) * 100, 2) if answered_calls else 0,
    })

# =========================
# Dashboard
# =========================
@app.get("/dashboard")
def dashboard():
    if sheet is None:
        return Response("<h1>Google Sheets not connected</h1>", mimetype="text/html; charset=utf-8", status=500)

    rows = sheet.get_all_records()
    total        = len(rows)
    answered     = sum(1 for r in rows if str(r.get("answered", "")).lower() == "yes")
    interested   = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")
    whatsapp_sent = sum(1 for r in rows if str(r.get("whatsapp_sent", "")).lower().startswith("yes"))

    conversion_total    = round((interested / total) * 100, 2) if total else 0
    conversion_answered = round((interested / answered) * 100, 2) if answered else 0

    recent_rows = list(reversed(rows[-20:])) if rows else []

    table_rows = ""
    for r in recent_rows:
        interested_badge = "🟢 כן" if str(r.get("interested", "")).lower() == "yes" else "🔴 לא"
        answered_badge   = "🟢 כן" if str(r.get("answered", "")).lower() == "yes" else "🔴 לא"
        wa_badge         = "🟢 נשלח" if str(r.get("whatsapp_sent", "")).lower().startswith("yes") else "⚪ לא"
        table_rows += f"""
        <tr>
            <td>{r.get("timestamp","")}</td>
            <td>{r.get("business_name","")}</td>
            <td>{r.get("phone_number","")}</td>
            <td>{answered_badge}</td>
            <td>{interested_badge}</td>
            <td>{wa_badge}</td>
            <td>{r.get("ended_reason","")}</td>
            <td style="max-width:420px;white-space:normal;">{r.get("summary","")}</td>
        </tr>"""

    html = f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
    <meta charset="utf-8">
    <title>Lead Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="font-family:Arial;padding:24px;">
    <h1>דשבורד לידים</h1>
    <p>סטטוס שיחות, התעניינות ושליחת וואטסאפ</p>
    <p><b>סה״כ שיחות:</b> {total}</p>
    <p><b>שיחות שנענו:</b> {answered}</p>
    <p><b>מתעניינים:</b> {interested}</p>
    <p><b>וואטסאפ נשלח:</b> {whatsapp_sent}</p>
    <p><b>המרה מכלל השיחות:</b> {conversion_total}%</p>
    <p><b>המרה מתוך מי שענה:</b> {conversion_answered}%</p>
    <h2>20 הלידים האחרונים</h2>
    <table border="1" cellpadding="6" cellspacing="0">
        <tr>
            <th>זמן</th><th>עסק</th><th>טלפון</th>
            <th>ענה</th><th>התעניין</th><th>וואטסאפ</th>
            <th>סיבת סיום</th><th>סיכום</th>
        </tr>
        {table_rows if table_rows else '<tr><td colspan="8">אין נתונים עדיין</td></tr>'}
    </table>
</body>
</html>"""

    return Response(html, mimetype="text/html; charset=utf-8")

# =========================
# Run
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

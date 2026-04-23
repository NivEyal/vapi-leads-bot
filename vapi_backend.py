from flask import Flask, request, jsonify
from datetime import datetime
import os
import re
from twilio.rest import Client
import gspread
from google.oauth2.service_account import Credentials
from flask import Response
from google.oauth2.service_account import Credentials
app = Flask(__name__)

# =========================
# ENV
# =========================
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")  # לדוגמה: whatsapp:+14155238886
VAPI_WEBHOOK_SECRET = os.getenv("VAPI_WEBHOOK_SECRET")

import os
from google.oauth2.service_account import Credentials
import gspread

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

google_service_account_info = {
    "type": os.getenv("GOOGLE_TYPE"),
    "project_id": os.getenv("GOOGLE_PROJECT_ID"),
    "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_I"),
    "private_key": os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n"),
    "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
    "client_id": os.getenv("GOOGLE_CLIENT_ID"),
    "auth_uri": os.getenv("GOOGLE_AUTH_URI"),
    "token_uri": os.getenv("GOOGLE_TOKEN_URI"),
    "auth_provider_x509_cert_url": os.getenv("GOOGLE_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_X509_CERT_URL"),
    "universe_domain": os.getenv("GOOGLE_UNIVERSE_DOMAIN", "googleapis.com"),
}

GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")

creds = Credentials.from_service_account_info(
    google_service_account_info,
    scopes=SCOPES
)

gs_client = gspread.authorize(creds)
sheet = gs_client.open_by_key(GOOGLE_SHEETS_ID).sheet1
# =========================
# Twilio client
# =========================
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# =========================
# Google Sheets client
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
try:
    creds = Credentials.from_service_account_info(
        google_service_account_info,
        scopes=SCOPES
    )

    gs_client = gspread.authorize(creds)
    sheet = gs_client.open_by_key(GOOGLE_SHEETS_ID).sheet1

except Exception as e:
    print("🔥 GOOGLE ERROR:", str(e))
    sheet = None

# =========================
# Helpers
# =========================

INTEREST_KEYWORDS = [
    "כן",
    "מעוניין",
    "תשלח",
    "שלח לי",
    "תשלחו",
    "וואטסאפ",
    "פרטים",
    "אשמח",
    "אשמח לקבל",
    "דבר איתי",
]

def is_interested(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k.lower() in t for k in INTEREST_KEYWORDS)

def normalize_phone_for_whatsapp(phone: str) -> str:
    # אם כבר בפורמט whatsapp:+972...
    if phone.startswith("whatsapp:"):
        return phone

    # ניקוי בסיסי
    digits = re.sub(r"[^\d+]", "", phone)

    # אם מתחיל ב-0 ישראלי, נהפוך ל-972
    if digits.startswith("0"):
        digits = "+972" + digits[1:]
    elif not digits.startswith("+"):
        digits = "+" + digits

    return f"whatsapp:{digits}"

def append_lead_row(row: list):
    sheet.append_row(row, value_input_option="USER_ENTERED")

def send_whatsapp(to_phone: str, business_name: str = "", summary: str = ""):
    to_whatsapp = normalize_phone_for_whatsapp(to_phone)

    body = (
        f"היי{(' ' + business_name) if business_name else ''}, תודה על השיחה.\n"
        f"כמו שביקשת, הנה הפרטים להמשך.\n"
        f"{summary}"
    )

    msg = twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=to_whatsapp,
        body=body
    )
    return msg.sid

# =========================
# Security
# =========================
def is_authorized(req) -> bool:
    auth_header = req.headers.get("Authorization", "")
    expected = f"Bearer {VAPI_WEBHOOK_SECRET}"
    return auth_header == expected

# =========================
# Webhook from Vapi
# =========================
@app.route("/healthz", methods=["GET"])
def healthz():
    return {"ok": True}, 200
@app.route("/", methods=["GET"])
def home():
    return {
        "status": "server running",
        "message": "Vapi backend is live"
    }, 200


@app.route("/webhooks/vapi", methods=["POST"])
def vapi_webhook():
    if not is_authorized(request):
        print("❌ UNAUTHORIZED")
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    print("🔥 FULL EVENT:", data)

    message = data.get("message", {})
    msg_type = message.get("type")
    print("🔥 MESSAGE TYPE:", msg_type)

    # המשך הקוד הקיים שלך...

    # ברירת מחדל
    if msg_type != "end-of-call-report":
        return ("", 204)

    call = message.get("call", {}) or {}
    artifact = message.get("artifact", {}) or {}

    call_id = call.get("id", "")
    phone_number = (
        call.get("customer", {}).get("number")
        or call.get("phoneNumber")
        or ""
    )
    business_name = call.get("customer", {}).get("name", "") or ""
    ended_reason = message.get("endedReason", "")
    transcript = artifact.get("transcript", "") or ""

    interested = is_interested(transcript)
    answered = ended_reason not in ["customer-did-not-answer", "assistant-not-available"]

    summary = "הלקוח הביע עניין וביקש פרטים בוואטסאפ." if interested else "לא זוהה עניין ברור."

    whatsapp_sent = "no"
    whatsapp_sid = ""

    # שולחים וואטסאפ רק אם יש עניין
    if interested and phone_number:
        try:
            whatsapp_sid = send_whatsapp(
                to_phone=phone_number,
                business_name=business_name,
                summary="נשמח להמשך שיחה ותיאום."
            )
            whatsapp_sent = "yes"
        except Exception as e:
            whatsapp_sent = f"failed: {str(e)[:120]}"

    row = [
        datetime.utcnow().isoformat(),
        call_id,
        phone_number,
        business_name,
        "yes" if answered else "no",
        "yes" if interested else "no",
        summary,
        transcript[:4000],   # כדי לא לפוצץ תא
        whatsapp_sent,
        whatsapp_sid,
        ended_reason,
    ]
    append_lead_row(row)

    return ("", 204)

# =========================
# Stats endpoint
# =========================
@app.route("/stats", methods=["GET"])
def stats():
    rows = sheet.get_all_records()

    total_calls = len(rows)
    answered_calls = sum(1 for r in rows if str(r.get("answered", "")).lower() == "yes")
    interested_calls = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")

    interest_rate_from_total = round((interested_calls / total_calls) * 100, 2) if total_calls else 0
    interest_rate_from_answered = round((interested_calls / answered_calls) * 100, 2) if answered_calls else 0

    return jsonify({
        "total_calls": total_calls,
        "answered_calls": answered_calls,
        "interested_calls": interested_calls,
        "interest_rate_from_total_pct": interest_rate_from_total,
        "interest_rate_from_answered_pct": interest_rate_from_answered,
    })
@app.route("/dashboard", methods=["GET"])
def dashboard():
    rows = sheet.get_all_records()

    total = len(rows)
    answered = sum(1 for r in rows if str(r.get("answered", "")).lower() == "yes")
    interested = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")
    whatsapp_sent = sum(1 for r in rows if str(r.get("whatsapp_sent", "")).lower().startswith("yes"))

    conversion_total = round((interested / total) * 100, 2) if total else 0
    conversion_answered = round((interested / answered) * 100, 2) if answered else 0

    recent_rows = rows[-20:] if rows else []
    recent_rows = list(reversed(recent_rows))

    table_rows = ""
    for r in recent_rows:
        interested_badge = "🟢 כן" if str(r.get("interested", "")).lower() == "yes" else "🔴 לא"
        answered_badge = "🟢 כן" if str(r.get("answered", "")).lower() == "yes" else "🔴 לא"
        wa_badge = "🟢 נשלח" if str(r.get("whatsapp_sent", "")).lower().startswith("yes") else "⚪ לא"

        table_rows += f"""
        <tr>
            <td>{r.get("timestamp", "")}</td>
            <td>{r.get("business_name", "")}</td>
            <td>{r.get("phone_number", "")}</td>
            <td>{answered_badge}</td>
            <td>{interested_badge}</td>
            <td>{wa_badge}</td>
            <td>{r.get("ended_reason", "")}</td>
            <td style="max-width:420px; white-space:normal;">{r.get("summary", "")}</td>
        </tr>
        """

    html = f"""
    <!doctype html>
    <html lang="he" dir="rtl">
    <head>
        <meta charset="utf-8">
        <title>Lead Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #f5f7fb;
                margin: 0;
                padding: 24px;
                color: #1f2937;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            h1 {{
                margin-bottom: 8px;
            }}
            .sub {{
                color: #6b7280;
                margin-bottom: 24px;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 16px;
                margin-bottom: 24px;
            }}
            .card {{
                background: white;
                border-radius: 16px;
                padding: 20px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.06);
            }}
            .label {{
                font-size: 14px;
                color: #6b7280;
                margin-bottom: 10px;
            }}
            .value {{
                font-size: 30px;
                font-weight: bold;
            }}
            .table-wrap {{
                background: white;
                border-radius: 16px;
                padding: 20px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.06);
                overflow-x: auto;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 14px;
            }}
            th, td {{
                text-align: right;
                padding: 12px 10px;
                border-bottom: 1px solid #e5e7eb;
                vertical-align: top;
            }}
            th {{
                background: #f9fafb;
                position: sticky;
                top: 0;
            }}
            .topbar {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 12px;
                flex-wrap: wrap;
                margin-bottom: 20px;
            }}
            .btn {{
                display: inline-block;
                text-decoration: none;
                background: #111827;
                color: white;
                padding: 10px 14px;
                border-radius: 10px;
                font-size: 14px;
            }}
            .muted {{
                color: #6b7280;
                font-size: 13px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="topbar">
                <div>
                    <h1>דשבורד לידים</h1>
                    <div class="sub">סטטוס שיחות, התעניינות ושליחת וואטסאפ</div>
                </div>
                <div>
                    <a class="btn" href="/stats" target="_blank">JSON Stats</a>
                </div>
            </div>

            <div class="grid">
                <div class="card">
                    <div class="label">סה״כ שיחות</div>
                    <div class="value">{total}</div>
                </div>
                <div class="card">
                    <div class="label">שיחות שנענו</div>
                    <div class="value">{answered}</div>
                </div>
                <div class="card">
                    <div class="label">מתעניינים</div>
                    <div class="value">{interested}</div>
                </div>
                <div class="card">
                    <div class="label">וואטסאפ נשלח</div>
                    <div class="value">{whatsapp_sent}</div>
                </div>
                <div class="card">
                    <div class="label">המרה מכלל השיחות</div>
                    <div class="value">{conversion_total}%</div>
                </div>
                <div class="card">
                    <div class="label">המרה מתוך מי שענה</div>
                    <div class="value">{conversion_answered}%</div>
                </div>
            </div>

            <div class="table-wrap">
                <h2 style="margin-top:0;">20 הלידים האחרונים</h2>
                <div class="muted" style="margin-bottom:12px;">חדש למעלה</div>
                <table>
                    <thead>
                        <tr>
                            <th>זמן</th>
                            <th>עסק</th>
                            <th>טלפון</th>
                            <th>ענה</th>
                            <th>התעניין</th>
                            <th>וואטסאפ</th>
                            <th>סיבת סיום</th>
                            <th>סיכום</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_rows if table_rows else '<tr><td colspan="8">אין נתונים עדיין</td></tr>'}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html; charset=utf-8")
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

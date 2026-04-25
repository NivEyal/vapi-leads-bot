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

EXPECTED_HEADERS = [
    "timestamp", "call_sid", "caller", "user_text", "interested",
    "sentiment", "lead_quality", "next_action", "summary",
    "whatsapp_sent", "whatsapp_sid", "stt_error", "openai_error", "tts_error",
]

def env(name, default=""):
    return os.getenv(name, default).strip()

PUBLIC_BASE_URL = env("PUBLIC_BASE_URL").rstrip("/")

TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = env("TWILIO_WHATSAPP_FROM")

XAI_API_KEY = env("XAI_API_KEY")
OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4.1-mini")

GOOGLE_SHEETS_ID = env("GOOGLE_SHEETS_ID")

GROK_TTS_VOICE = env("GROK_TTS_VOICE", "leo")
GROK_TTS_LANGUAGE = env("GROK_TTS_LANGUAGE", "he")

AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# =========================
# Twilio
# =========================
twilio_client = None
try:
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
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
    creds = Credentials.from_service_account_info(google_service_account_info, scopes=SCOPES)
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

def ensure_headers():
    if sheet is None:
        return False
    try:
        current = sheet.row_values(1)
        if current != EXPECTED_HEADERS:
            sheet.update("A1:N1", [EXPECTED_HEADERS])
            print("✅ Google Sheets headers fixed", flush=True)
        return True
    except Exception as e:
        print("🔥 HEADER FIX ERROR:", str(e), flush=True)
        return False

def get_sheet_rows():
    if sheet is None:
        return []
    ensure_headers()
    try:
        return sheet.get_all_records(expected_headers=EXPECTED_HEADERS)
    except Exception as e:
        print("🔥 GET ROWS ERROR:", str(e), flush=True)
        return []

def append_lead_row(row):
    if sheet is None:
        print("⚠️ Google Sheet not connected", flush=True)
        return False
    ensure_headers()
    sheet.append_row(row, value_input_option="USER_ENTERED")
    return True

def normalize_phone_for_whatsapp(phone):
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
        match = re.search(r"\{.*\}", text or "", re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return {}

# =========================
# ✅ IMPROVED: Hebrew "yes" detection
# =========================

# מילות כן - word-boundary aware
YES_WORDS = [
    "כן", "בטח", "ברור", "אפשר", "יאללה", "סבבה", "אוקיי", "אוקי",
    "שלח", "תשלח", "תשלחי", "שלחו", "וואטסאפ", "ווטסאפ", "ווצאפ",
    "מעוניין", "מעוניינת", "רוצה", "אשמח", "פרטים", "יכול",
    "yes", "yeah", "yep", "ok", "okay", "sure", "please",
]

# מילות לא - חייבות להופיע כמילה שלמה
NO_WORDS = [
    "לא מעוניין", "לא מעוניינת", "לא רלוונטי", "לא תודה",
    "אל תשלח", "לא לשלוח", "תוריד", "עזוב",
    "no thanks", "not interested",
]

def detect_interest(text: str) -> bool:
    """
    מחזיר True אם הטקסט מכיל עניין.
    - בודק מילות 'לא' תחילה כמילים שלמות
    - אז בודק מילות 'כן' - כולל word-boundary כדי למנוע false positives
    """
    if not text:
        return False

    clean = text.strip().lower()
    # נרמול תווים מיוחדים
    clean = re.sub(r"[^\w\u0590-\u05FF\s]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    print(f"🔍 DETECT INTEREST INPUT: '{clean}'", flush=True)

    # בדוק סירוב - כביטויים שלמים
    for phrase in NO_WORDS:
        if phrase in clean:
            print(f"❌ NO match: '{phrase}'", flush=True)
            return False

    # מניעת false positive: "לא" לבד כמילה שלמה = סירוב
    if re.search(r"(?<!\S)לא(?!\S)", clean):
        # אם יש "לא" לבד אבל גם מילת כן אחרת - כן מנצח
        # לדוגמה: "לא, רגע, כן בטח" -> כן
        has_strong_yes = any(
            re.search(rf"(?<!\S){re.escape(w)}(?!\S)", clean)
            for w in ["כן", "בטח", "ברור", "אשמח", "רוצה", "yes", "ok", "okay", "sure"]
        )
        if not has_strong_yes:
            print(f"❌ Standalone 'לא' detected, no strong yes override", flush=True)
            return False

    # בדוק עניין
    for word in YES_WORDS:
        # word-boundary: לא חלק ממילה אחרת
        pattern = rf"(?<!\S){re.escape(word)}(?!\S)"
        if re.search(pattern, clean):
            print(f"✅ YES match: '{word}'", flush=True)
            return True

    print(f"⚪ No match found - defaulting to False", flush=True)
    return False


# =========================
# WhatsApp
# =========================
def send_whatsapp(to_phone, summary=""):
    if twilio_client is None:
        raise RuntimeError("Twilio client not initialized")
    if not TWILIO_WHATSAPP_FROM:
        raise RuntimeError("TWILIO_WHATSAPP_FROM missing")

    msg = twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=normalize_phone_for_whatsapp(to_phone),
        body=(
            "היי, תודה על השיחה 🙏\n\n"
            "כמו שביקשת, הנה הפרטים להמשך:\n"
            f"{summary}\n\n"
            "נשמח לעזור ולתאם המשך."
        ),
    )
    return msg.sid

# =========================
# ✅ TTS Cache - מונע קריאות חוזרות לאותן הודעות
# =========================
_tts_cache: dict[str, str] = {}

def grok_tts(text: str) -> str:
    """מחזיר URL לקובץ אודיו. שומר cache לפי טקסט."""
    if text in _tts_cache:
        # וודא שהקובץ עדיין קיים
        cached_url = _tts_cache[text]
        file_name = cached_url.split("/")[-1]
        if os.path.exists(os.path.join(AUDIO_DIR, file_name)):
            print(f"🎵 TTS CACHE HIT", flush=True)
            return cached_url

    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY missing")

    payload = {
        "text": text[:1500],
        "voice_id": GROK_TTS_VOICE,
        "language": GROK_TTS_LANGUAGE,
        "format": "mp3",
    }

    res = requests.post(
        "https://api.x.ai/v1/tts",
        headers={
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=45,
    )

    print("🔥 GROK TTS STATUS:", res.status_code, res.headers.get("content-type"), flush=True)
    if res.status_code != 200:
        print("🔥 GROK TTS BODY:", res.text[:300], flush=True)
        raise RuntimeError(f"Grok TTS error {res.status_code}: {res.text[:300]}")

    file_id = str(uuid.uuid4())
    file_name = f"{file_id}.mp3"
    path = os.path.join(AUDIO_DIR, file_name)

    with open(path, "wb") as f:
        f.write(res.content)

    url = f"{PUBLIC_BASE_URL}/audio/{file_name}"
    _tts_cache[text] = url
    return url


# =========================
# ✅ Pre-warm TTS cache at startup
# =========================
GREETING_TEXT = (
    "שלום, מדבר עוזר דיגיטלי שעוזר לעסקים לא לפספס לקוחות. "
    "תרצה שאשלח לך פרטים בוואטסאפ? תגיד כן."
)
SUCCESS_TEXT = "מעולה, שלחתי לך הודעה בוואטסאפ. תודה ולהתראות."
RETRY_TEXT = "לא שמעתי תשובה ברורה. תגיד כן אם תרצה שאשלח לך פרטים בוואטסאפ."
FINAL_TEXT = "תודה רבה ולהתראות."

def prewarm_tts():
    """מייצר את קבצי האודיו הקבועים בעת הפעלה כדי לחסוך זמן בשיחה."""
    if not XAI_API_KEY:
        print("⚠️ Skipping TTS prewarm - no XAI_API_KEY", flush=True)
        return
    for text in [GREETING_TEXT, SUCCESS_TEXT, RETRY_TEXT, FINAL_TEXT]:
        try:
            grok_tts(text)
            print(f"🎵 Pre-warmed TTS: {text[:40]}...", flush=True)
        except Exception as e:
            print(f"⚠️ TTS prewarm failed: {e}", flush=True)


# =========================
# Grok STT
# =========================
def grok_stt(recording_url):
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY missing")

    audio_res = requests.get(
        recording_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=25,
    )

    if audio_res.status_code != 200:
        raise RuntimeError(f"Could not download recording: {audio_res.status_code}")

    res = requests.post(
        "https://api.x.ai/v1/stt",
        headers={"Authorization": f"Bearer {XAI_API_KEY}"},
        files={"file": ("recording.wav", audio_res.content, "audio/wav")},
        timeout=45,
    )

    print("🔥 GROK STT STATUS:", res.status_code, res.headers.get("content-type"), flush=True)

    if res.status_code != 200:
        raise RuntimeError(f"Grok STT error {res.status_code}: {res.text[:300]}")

    return res.json().get("text", "").strip()


@app.get("/audio/<file_name>")
def serve_audio(file_name):
    path = os.path.join(AUDIO_DIR, file_name)

    if not os.path.exists(path):
        return Response("not found", status=404)

    with open(path, "rb") as f:
        audio = f.read()

    return Response(
        audio,
        mimetype="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# =========================
# OpenAI Analyze (optional - not used in main flow)
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

חובה להחזיר JSON בלבד:
{
  "reply": "תשובה קולית קצרה ללקוח",
  "interested": true,
  "sentiment": "positive / neutral / negative",
  "summary": "סיכום קצר של השיחה",
  "next_action": "send_whatsapp / continue_call / no_action",
  "lead_quality": "hot / warm / cold"
}
"""

def openai_analyze(user_text, caller=""):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"מספר לקוח: {caller}\n\nהלקוח אמר:\n{user_text}",
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
        timeout=45,
    )

    if res.status_code != 200:
        raise RuntimeError(f"OpenAI error {res.status_code}: {res.text[:300]}")

    text = res.json().get("output_text", "").strip()
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
# ✅ FIXED: Voice Start
# הברכה + ההאזנה בתוך Gather אחד - Twilio מאזין כבר בזמן ההשמעה
# =========================
HINTS = (
    "כן,בטח,ברור,סבבה,שלח,תשלח,וואטסאפ,yes,yeah,ok,okay,sure,"
    "אשמח,רוצה,מעוניין,מעוניינת,אפשר,יאללה,פרטים,נשמח"
)

@app.post("/voice")
def voice():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")

    print(f"📞 Incoming call from {caller} | {call_sid}", flush=True)

    r = VoiceResponse()

    # ✅ הכל בתוך gather אחד:
    # Twilio מתחיל להאזין מיד עם תחילת ההשמעה.
    # אם המשתמש מדבר תוך כדי - זה נתפס!
    gather = r.gather(
        input="speech",
        action="/gather",
        method="POST",
        language="he-IL",
        speech_timeout=3,        # שניות שקט אחרי דיבור → סיום
        timeout=15,              # שניות המתנה לתחילת דיבור
        hints=HINTS,
    )

    # הברכה בתוך ה-gather
    try:
        audio_url = grok_tts(GREETING_TEXT)
        print("🔊 GREETING AUDIO (in gather):", audio_url, flush=True)
        gather.play(audio_url)
    except Exception as e:
        print("🔥 GREETING TTS ERROR:", str(e), flush=True)
        gather.say(GREETING_TEXT, language="he-IL")

    # אם timeout עבר בלי דיבור - עבור ל-voice-timeout
    r.redirect("/voice-timeout")

    return Response(str(r), mimetype="text/xml")


@app.post("/voice-timeout")
def voice_timeout():
    r = VoiceResponse()

    # ניסיון שני - גם כאן הכל בתוך gather
    gather = r.gather(
        input="speech",
        action="/gather",
        method="POST",
        language="he-IL",
        speech_timeout=3,
        timeout=12,
        hints=HINTS,
    )

    try:
        gather.play(grok_tts(RETRY_TEXT))
    except Exception:
        gather.say(RETRY_TEXT, language="he-IL")

    # אם עדיין אין תגובה - סיום
    try:
        r.play(grok_tts(FINAL_TEXT))
    except Exception:
        r.say(FINAL_TEXT, language="he-IL")

    r.hangup()
    return Response(str(r), mimetype="text/xml")


# =========================
# ✅ IMPROVED: Gather - זיהוי עניין משופר + לוג מפורט
# =========================
@app.post("/gather")
def gather():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")
    speech_result = request.form.get("SpeechResult", "")

    print(f"🎤 SPEECH RESULT RAW: {repr(speech_result)}", flush=True)

    interested = detect_interest(speech_result)

    print(f"📊 INTERESTED: {interested} | caller: {caller}", flush=True)

    whatsapp_sent = "no"
    whatsapp_sid = ""
    summary = "הלקוח ביקש לקבל פרטים בוואטסאפ." if interested else "לא זוהה עניין ברור."

    # שלח וואטסאפ אם מעוניין
    if interested and caller:
        try:
            whatsapp_sid = send_whatsapp(
                to_phone=caller,
                summary="נשמח להמשך שיחה ותיאום."
            )
            whatsapp_sent = "yes"
            print(f"✅ WhatsApp sent to {caller} | SID: {whatsapp_sid}", flush=True)
        except Exception as e:
            whatsapp_sent = f"failed: {str(e)[:120]}"
            print("🔥 WHATSAPP ERROR:", str(e), flush=True)

    # שמור ב-Sheets ב-background (לא חוסם את התגובה)
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
        whatsapp_sent,
        whatsapp_sid,
        "",
        "",
        "",
    ]

    try:
        append_lead_row(row)
    except Exception as e:
        print("🔥 SHEET APPEND ERROR:", str(e), flush=True)

    r = VoiceResponse()

    if interested:
        if whatsapp_sent == "yes":
            try:
                r.play(grok_tts(SUCCESS_TEXT))
            except Exception:
                r.say(SUCCESS_TEXT, language="he-IL")
        else:
            r.say(
                "קיבלתי את האישור שלך. הייתה בעיה בשליחת הוואטסאפ, נחזור אליך בהקדם.",
                language="he-IL",
            )
        r.hangup()

    else:
        # ניסיון נוסף אחד - אם עדיין לא ברור, נסיים
        gather_retry = r.gather(
            input="speech",
            action="/gather-final",
            method="POST",
            language="he-IL",
            speech_timeout="auto",
            timeout=6,
            hints="כן,בטח,ברור,שלח,תשלח,וואטסאפ,yes,ok,sure,אשמח,רוצה",
        )
        gather_retry.say(
            "לא קלטתי. תגיד כן אם תרצה שאשלח לך פרטים בוואטסאפ.",
            language="he-IL",
        )

        try:
            r.play(grok_tts(FINAL_TEXT))
        except Exception:
            r.say(FINAL_TEXT, language="he-IL")
        r.hangup()

    return Response(str(r), mimetype="text/xml")


# =========================
# ✅ NEW: Gather Final - ניסיון אחרון
# =========================
@app.post("/gather-final")
def gather_final():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")
    speech_result = request.form.get("SpeechResult", "")

    print(f"🎤 GATHER FINAL RAW: {repr(speech_result)}", flush=True)

    interested = detect_interest(speech_result)

    whatsapp_sent = "no"
    whatsapp_sid = ""

    if interested and caller:
        try:
            whatsapp_sid = send_whatsapp(
                to_phone=caller,
                summary="נשמח להמשך שיחה ותיאום."
            )
            whatsapp_sent = "yes"
        except Exception as e:
            whatsapp_sent = f"failed: {str(e)[:120]}"
            print("🔥 WHATSAPP ERROR (final):", str(e), flush=True)

    summary = "הלקוח ביקש לקבל פרטים (ניסיון שני)." if interested else "לא זוהה עניין - סיום שיחה."

    row = [
        now_iso(),
        call_sid + "_retry",
        caller,
        speech_result,
        "yes" if interested else "no",
        "positive" if interested else "neutral",
        "hot" if interested else "cold",
        "send_whatsapp" if interested else "no_action",
        summary,
        whatsapp_sent,
        whatsapp_sid,
        "",
        "",
        "",
    ]

    try:
        append_lead_row(row)
    except Exception as e:
        print("🔥 SHEET APPEND ERROR (final):", str(e), flush=True)

    r = VoiceResponse()

    if interested:
        try:
            r.play(grok_tts(SUCCESS_TEXT))
        except Exception:
            r.say(SUCCESS_TEXT, language="he-IL")
    else:
        try:
            r.play(grok_tts(FINAL_TEXT))
        except Exception:
            r.say(FINAL_TEXT, language="he-IL")

    r.hangup()
    return Response(str(r), mimetype="text/xml")


# =========================
# Process Recording (STT flow)
# =========================
@app.post("/process")
def process():
    caller = request.form.get("From", "")
    call_sid = request.form.get("CallSid", "")
    recording_url = request.form.get("RecordingUrl", "")

    user_text = ""
    stt_error = ""
    whatsapp_sent = "no"
    whatsapp_sid = ""

    try:
        if recording_url:
            user_text = grok_stt(recording_url + ".wav")
    except Exception as e:
        stt_error = str(e)
        print("🔥 STT ERROR:", stt_error, flush=True)

    print("🧠 USER TEXT RAW:", repr(user_text), flush=True)

    interested = detect_interest(user_text)

    summary = "הלקוח ביקש לקבל פרטים בוואטסאפ." if interested else "לא זוהה עניין ברור."

    if interested and caller:
        try:
            whatsapp_sid = send_whatsapp(
                to_phone=caller,
                summary="נשמח להמשך שיחה ותיאום."
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
        "positive" if interested else "neutral",
        "hot" if interested else "cold",
        "send_whatsapp" if interested else "no_action",
        summary,
        whatsapp_sent,
        whatsapp_sid,
        stt_error,
        "",
        "",
    ]

    try:
        append_lead_row(row)
    except Exception as e:
        print("🔥 SHEET APPEND ERROR:", str(e), flush=True)

    r = VoiceResponse()

    if interested:
        if whatsapp_sent == "yes":
            try:
                r.play(grok_tts(SUCCESS_TEXT))
            except Exception:
                r.say(SUCCESS_TEXT, language="he-IL")
        else:
            r.say(
                "קיבלתי את האישור שלך. הייתה בעיה בשליחת הוואטסאפ, אבל שמרתי את הפרטים ונחזור אליך.",
                language="he-IL",
            )
        r.hangup()

    else:
        r.say("לא קלטתי אישור ברור. אם תרצה פרטים, תגיד כן.", language="he-IL")
        r.record(
            action="/process",
            method="POST",
            max_length=6,
            timeout=4,
            play_beep=False,
            transcribe=False,
        )
        try:
            r.play(grok_tts(FINAL_TEXT))
        except Exception:
            r.say(FINAL_TEXT, language="he-IL")
        r.hangup()

    return Response(str(r), mimetype="text/xml")


# =========================
# Test WhatsApp
# =========================
@app.get("/test-whatsapp")
def test_whatsapp():
    to = request.args.get("to", "").strip()

    if not to:
        return jsonify({"ok": False, "error": "missing ?to=+972..."})

    try:
        sid = send_whatsapp(to_phone=to, summary="זו הודעת בדיקה מהמערכת.")
        return jsonify({"ok": True, "sid": sid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================
# ✅ NEW: Debug endpoint - לבדוק זיהוי מילים
# =========================
@app.get("/test-detect")
def test_detect():
    text = request.args.get("text", "").strip()
    result = detect_interest(text)
    return jsonify({"text": text, "interested": result})


# =========================
# Stats
# =========================
@app.get("/stats")
def stats():
    rows = get_sheet_rows()

    total = len(rows)
    interested = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")
    whatsapp_sent = sum(
        1 for r in rows
        if str(r.get("whatsapp_sent", "")).lower().startswith("yes")
    )

    return jsonify({
        "ok": True,
        "total_calls": total,
        "interested": interested,
        "whatsapp_sent": whatsapp_sent,
        "conversion_pct": round((interested / total) * 100, 2) if total else 0,
    })


# =========================
# Dashboard
# =========================
@app.get("/dashboard")
def dashboard():
    rows = get_sheet_rows()

    total = len(rows)
    interested = sum(1 for r in rows if str(r.get("interested", "")).lower() == "yes")
    whatsapp_sent = sum(
        1 for r in rows
        if str(r.get("whatsapp_sent", "")).lower().startswith("yes")
    )

    conversion = round((interested / total) * 100, 2) if total else 0
    recent_rows = list(reversed(rows[-30:]))

    table_rows = ""
    for r in recent_rows:
        interested_badge = "🟢 כן" if str(r.get("interested", "")).lower() == "yes" else "🔴 לא"
        wa_badge = "🟢 נשלח" if str(r.get("whatsapp_sent", "")).lower().startswith("yes") else "⚪ לא"

        table_rows += f"""
        <tr>
            <td>{r.get("timestamp", "")}</td>
            <td>{r.get("caller", "")}</td>
            <td>{interested_badge}</td>
            <td>{wa_badge}</td>
            <td>{r.get("lead_quality", "")}</td>
            <td>{r.get("sentiment", "")}</td>
            <td>{r.get("summary", "")}</td>
            <td>{r.get("user_text", "")}</td>
        </tr>
        """

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
}}
table {{
    margin-top: 24px;
    width: 100%;
    border-collapse: collapse;
    background: white;
}}
th, td {{
    padding: 10px;
    border-bottom: 1px solid #eee;
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
    <div class="card"><div>סה״כ שיחות</div><div class="num">{total}</div></div>
    <div class="card"><div>מתעניינים</div><div class="num">{interested}</div></div>
    <div class="card"><div>וואטסאפ נשלח</div><div class="num">{whatsapp_sent}</div></div>
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
    ensure_headers()
    return jsonify({
        "ok": True,
        "service": "AI sales call bot",
        "tts_voice": GROK_TTS_VOICE,
        "tts_language": GROK_TTS_LANGUAGE,
        "routes": ["/voice", "/dashboard", "/stats", "/test-whatsapp?to=+972...", "/test-detect?text=כן בטח"],
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

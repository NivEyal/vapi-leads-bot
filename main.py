import os
import json
import base64
import requests

from fastapi import FastAPI, WebSocket, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client

app = FastAPI()

BASE_URL = os.getenv("PUBLIC_BASE_URL", "").replace("https://", "").replace("http://", "").rstrip("/")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()

TWILIO_CLIENT = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

YES_WORDS = [
    "כן", "בטח", "ברור", "סבבה", "אפשר", "תשלח", "שלח",
    "אוקיי", "אוקי", "יאללה", "מעוניין", "אשמח",
    "yes", "yeah", "yep", "ok", "okay", "sure", "send"
]

NO_WORDS = ["לא תודה", "לא מעוניין", "no thanks", "not interested", "לא", "no", "nope"]


def normalize_whatsapp_number(phone: str) -> str:
    if not phone:
        return ""

    phone = phone.replace(" ", "").replace("-", "")

    if phone.startswith("whatsapp:"):
        return phone
    if phone.startswith("0"):
        phone = "+972" + phone[1:]
    elif phone.startswith("972"):
        phone = "+" + phone
    elif not phone.startswith("+"):
        phone = "+" + phone

    return f"whatsapp:{phone}"


def detect_interest(text: str) -> bool:
    text = (text or "").lower().strip()

    for w in NO_WORDS:
        if w in text:
            return False

    for w in YES_WORDS:
        if w in text:
            return True

    return False


def send_whatsapp(to_number: str) -> bool:
    try:
        from_number = TWILIO_WHATSAPP_FROM
        if from_number and not from_number.startswith("whatsapp:"):
            from_number = f"whatsapp:{from_number}"

        TWILIO_CLIENT.messages.create(
            from_=from_number,
            to=normalize_whatsapp_number(to_number),
            body="היי! הנה הפרטים על העוזר הדיגיטלי ב-10 שקלים ליום. נשמח לתאם שיחה קצרה.",
        )

        print("✅ WhatsApp sent", flush=True)
        return True

    except Exception as e:
        print("❌ WhatsApp error:", e, flush=True)
        return False


def google_stt_from_mulaw(mulaw_bytes: bytes) -> str:
    url = "https://speech.googleapis.com/v1/speech:recognize"

    audio_base64 = base64.b64encode(mulaw_bytes).decode("utf-8")

    payload = {
        "config": {
            "encoding": "MULAW",
            "sampleRateHertz": 8000,
            "languageCode": "he-IL",
            "alternativeLanguageCodes": ["en-US"],
            "enableAutomaticPunctuation": False,
        },
        "audio": {
            "content": audio_base64
        }
    }

    res = requests.post(
        f"{url}?key={GOOGLE_API_KEY}",
        json=payload,
        timeout=15,
    )

    if res.status_code != 200:
        print("❌ Google STT HTTP error:", res.text[:500], flush=True)
        return ""

    data = res.json()
    results = data.get("results", [])

    texts = []
    for result in results:
        alternatives = result.get("alternatives", [])
        if alternatives:
            texts.append(alternatives[0].get("transcript", ""))

    return " ".join(texts).strip()


@app.get("/")
def home():
    return {
        "ok": True,
        "service": "Twilio Stream + Google STT",
        "base_url": BASE_URL,
        "google_key_exists": bool(GOOGLE_API_KEY),
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    caller = form.get("From", "Unknown")

    resp = VoiceResponse()

    resp.say(
        "שלום, אני עוזר דיגיטלי ב-10 שקלים ליום. תרצה שאשלח לך פרטים בוואטסאפ?",
        language="he-IL"
    )

    connect = Connect()
    stream = Stream(url=f"wss://{BASE_URL}/media-stream")
    stream.parameter(name="caller", value=caller)
    connect.append(stream)
    resp.append(connect)

    return Response(str(resp), media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    stream_sid = None
    caller = None
    audio_buffer = bytearray()
    whatsapp_sent = False

    print("🚀 Twilio WebSocket connected", flush=True)

    try:
        async for message in websocket.iter_text():
            data = json.loads(message)
            event = data.get("event")

            if event == "start":
                stream_sid = data["start"]["streamSid"]
                caller = data["start"]["customParameters"].get("caller")
                print("📞 Call from:", caller, "| stream:", stream_sid, flush=True)
                audio_buffer.clear()

            elif event == "media":
                payload = data["media"]["payload"]
                chunk = base64.b64decode(payload)
                audio_buffer.extend(chunk)

                # בערך 1.5 שניות באודיו μ-law 8k
                if len(audio_buffer) >= 12000:
                    current_audio = bytes(audio_buffer)
                    audio_buffer.clear()

                    text = google_stt_from_mulaw(current_audio)
                    print("🧠 GOOGLE STT:", repr(text), flush=True)

                    if not text:
                        continue

                    if detect_interest(text):
                        if not whatsapp_sent:
                            whatsapp_sent = send_whatsapp(caller)

                        # מנתקים את ה-stream כדי להמשיך ל-TwiML רגיל
                        await websocket.close()
                        break

                    # אם לא זיהה כן/לא, ממשיכים להקשיב לעוד chunk

            elif event in ["stop", "close"]:
                print("⏹️ Twilio stream stopped", flush=True)
                break

    except Exception as e:
        print("🔥 WS error:", e, flush=True)

    finally:
        try:
            await websocket.close()
        except Exception:
            pass

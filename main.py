import os
import json
import base64
import asyncio
import audioop
import requests
import websockets

from fastapi import FastAPI, WebSocket, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client

app = FastAPI()

BASE_URL = os.getenv("PUBLIC_BASE_URL", "").replace("https://", "").replace("http://", "").rstrip("/")
XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()

TWILIO_CLIENT = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

SYSTEM_PROMPT = """
אתה עוזר מכירות טלפוני בעברית.
דבר קצר וטבעי.
אם הלקוח מאשר: כן, בטח, סבבה, תשלח, אפשר, אוקיי, yes, yeah, ok, sure —
זה אישור לשלוח וואטסאפ.
אם הלקוח מסרב: לא, לא תודה, no —
אל תשלח וואטסאפ.
"""

YES_WORDS = [
    "כן", "בטח", "ברור", "סבבה", "אפשר", "תשלח", "שלח",
    "אוקיי", "אוקי", "יאללה", "מעוניין", "אשמח",
    "yes", "yeah", "yep", "ok", "okay", "sure", "send"
]

NO_WORDS = ["לא", "לא תודה", "לא מעוניין", "no", "nope", "no thanks"]


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

    if any(w in text for w in NO_WORDS):
        return False

    return any(w in text for w in YES_WORDS)


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
        f"{url}?key={os.getenv('GOOGLE_API_KEY')}",
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


def grok_tts_ulaw(text: str) -> bytes:
    url = "https://api.x.ai/v1/tts"

    payload = {
        "text": text,
        "voice_id": "leo",
        "language": "he",
        "format": "mulaw",
        "sample_rate": 8000,
    }

    res = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )

    if res.status_code != 200:
        raise RuntimeError(f"Grok TTS error {res.status_code}: {res.text[:300]}")

    return res.content


async def send_audio_to_twilio(websocket: WebSocket, stream_sid: str, audio_bytes: bytes):
    """
    Twilio expects base64 μ-law 8k audio chunks.
    """
    chunk_size = 160  # 20ms at 8k μ-law

    for i in range(0, len(audio_bytes), chunk_size):
        chunk = audio_bytes[i:i + chunk_size]
        payload = base64.b64encode(chunk).decode("utf-8")

        await websocket.send_json({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload},
        })

        await asyncio.sleep(0.02)


async def speak(websocket: WebSocket, stream_sid: str, text: str):
    try:
        audio = grok_tts_ulaw(text)
        await send_audio_to_twilio(websocket, stream_sid, audio)
    except Exception as e:
        print("❌ TTS error:", e, flush=True)


@app.get("/")
def home():
    return {
        "ok": True,
        "service": "Twilio + Google STT + Grok Leo TTS",
        "base_url": BASE_URL,
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    caller = form.get("From", "Unknown")

    resp = VoiceResponse()
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
    bot_spoke = False
    whatsapp_sent = False
    speaking = False

    print("🚀 Twilio WebSocket connected", flush=True)

    try:
        async for message in websocket.iter_text():
            data = json.loads(message)
            event = data.get("event")

            if event == "start":
                stream_sid = data["start"]["streamSid"]
                caller = data["start"]["customParameters"].get("caller")

                print("📞 Call from:", caller, flush=True)

                await speak(
                    websocket,
                    stream_sid,
                    "שלום, אני עוזר דיגיטלי ב-10 שקלים ליום. תרצה שאשלח לך פרטים בוואטסאפ?"
                )

                bot_spoke = True
                audio_buffer.clear()

            elif event == "media":
                if not bot_spoke or speaking:
                    continue

                payload = data["media"]["payload"]
                chunk = base64.b64decode(payload)

                audio_buffer.extend(chunk)

                # בערך 1.8 שניות אודיו: 8000 bytes/sec μ-law
                if len(audio_buffer) >= 14500:
                    current_audio = bytes(audio_buffer)
                    audio_buffer.clear()

                    try:
                        text = google_stt_from_mulaw(current_audio)
                        print("🧠 GOOGLE STT:", repr(text), flush=True)
                    except Exception as e:
                        print("❌ Google STT error:", e, flush=True)
                        continue

                    if not text:
                        continue

                    if detect_interest(text):
                        if not whatsapp_sent:
                            whatsapp_sent = send_whatsapp(caller)

                        if whatsapp_sent:
                            await speak(websocket, stream_sid, "מעולה, שלחתי לך עכשיו הודעה בוואטסאפ.")
                        else:
                            await speak(websocket, stream_sid, "מעולה, קיבלתי אישור. הייתה בעיה בשליחת הוואטסאפ, אבל שמרתי את הפרטים.")

                        break

                    else:
                        await speak(websocket, stream_sid, "רק לוודא, לשלוח לך פרטים בוואטסאפ?")
                        audio_buffer.clear()

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

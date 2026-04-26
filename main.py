import os
import json
import asyncio
import base64
import websockets

from fastapi import FastAPI, WebSocket, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client

try:
    import audioop
except ImportError:
    try:
        from audioop_lts import audioop
    except ImportError:
        audioop = None

app = FastAPI()

BASE_URL = os.getenv("PUBLIC_BASE_URL", "").replace("https://", "").replace("http://", "").rstrip("/")
XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()

TWILIO_CLIENT = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


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


def send_whatsapp_logic(to_number: str) -> bool:
    if not to_number:
        print("⚠️ No phone number", flush=True)
        return False

    try:
        from_number = TWILIO_WHATSAPP_FROM
        if from_number and not from_number.startswith("whatsapp:"):
            from_number = f"whatsapp:{from_number}"

        to_number = normalize_whatsapp_number(to_number)

        TWILIO_CLIENT.messages.create(
            from_=from_number,
            to=to_number,
            body=(
                "שלום! הנה הפרטים על העוזר הדיגיטלי ב-10 שקלים ליום. "
                "נשמח לתאם שיחה קצרה."
            ),
        )

        print(f"✅ WhatsApp sent to {to_number}", flush=True)
        return True

    except Exception as e:
        print(f"❌ WhatsApp Error: {e}", flush=True)
        return False


@app.get("/")
def home():
    return {
        "ok": True,
        "service": "Twilio Media Streams + Grok Realtime",
        "base_url": BASE_URL,
    }


@app.get("/healthz")
def healthz():
    return {"status": "healthy"}


@app.post("/voice")
async def handle_voice(request: Request):
    form = await request.form()
    caller = form.get("From", "Unknown")

    resp = VoiceResponse()
    connect = Connect()

    stream = Stream(url=f"wss://{BASE_URL}/media-stream")
    stream.parameter(name="caller", value=caller)

    connect.append(stream)
    resp.append(connect)

    return Response(content=str(resp), media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("🚀 Twilio WebSocket Connected", flush=True)

    xai_url = "wss://api.x.ai/v1/realtime"
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}"
    }

    try:
        async with websockets.connect(
            xai_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        ) as xai_ws:
            print("✅ Connected to xAI Realtime", flush=True)

            stream_sid = None
            phone_number = None
            resample_state = None
            whatsapp_sent_once = False

            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": """
אתה עוזר דיגיטלי עסקי בשם גרוק.

חובה לדבר בעברית טבעית וקצרה.

משפט פתיחה:
אמור בדיוק:
"שלום, אני עוזר דיגיטלי ב-10 שקלים ליום. תרצה שאשלח לך פרטים בוואטסאפ?"

זיהוי אישור:
אם הלקוח אומר אחד מהבאים:
כן, בטח, ברור, סבבה, אפשר, תשלח, שלח, אוקיי, אוקי, יאללה, מעוניין, אשמח
או באנגלית:
yes, yeah, yep, ok, okay, sure, send

זה נחשב אישור.

כאשר יש אישור:
1. חובה להפעיל מיד את הכלי send_whatsapp.
2. רק אחרי שהכלי הופעל, אמור:
"מעולה, שלחתי לך עכשיו הודעה בוואטסאפ."

אם הלקוח אומר:
לא, לא תודה, לא מעוניין, no, nope, no thanks
אז אל תפעיל את הכלי ואמור:
"אין בעיה, תודה ולהתראות."

אם לא ברור:
שאל:
"רק לוודא, לשלוח לך פרטים בוואטסאפ?"

אל תאמר ששלחת וואטסאפ אם לא הפעלת את הכלי.
אל תנתק לפני שסיימת לדבר.
""",
                    "voice": "leo",
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "pcm16",
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.4,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 800,
                    },
                    "tools": [
                        {
                            "type": "function",
                            "name": "send_whatsapp",
                            "description": "שולח הודעת וואטסאפ עם פרטים ללקוח אחרי שהלקוח אישר בקול.",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                            },
                        }
                    ],
                },
            }

            await xai_ws.send(json.dumps(session_update))

            async def xai_to_twilio():
                nonlocal stream_sid, resample_state, whatsapp_sent_once

                async for message in xai_ws:
                    response = json.loads(message)
                    event_type = response.get("type")

                    print("XAI EVENT:", event_type, flush=True)

                    if "transcript" in response:
                        print("🧠 TRANSCRIPT:", response.get("transcript"), flush=True)

                    if event_type in [
                        "conversation.item.input_audio_transcription.completed",
                        "input_audio_transcription.completed",
                    ]:
                        print("🧠 USER TRANSCRIPT FULL:", response, flush=True)

                    if event_type == "error":
                        print("❌ XAI ERROR:", response, flush=True)

                    # Barge-in
                    if event_type == "input_audio_buffer.speech_started" and stream_sid:
                        await websocket.send_json({
                            "event": "clear",
                            "streamSid": stream_sid,
                        })

                    # Audio: xAI pcm16 24khz -> Twilio mulaw 8khz
                    if event_type in ["response.audio.delta", "response.output_audio.delta"]:
                        payload = response.get("delta") or response.get("audio")

                        if payload and stream_sid and audioop:
                            try:
                                pcm_data = base64.b64decode(payload)

                                resampled_data, resample_state = audioop.ratecv(
                                    pcm_data,
                                    2,
                                    1,
                                    24000,
                                    8000,
                                    resample_state,
                                )

                                mu_law_data = audioop.lin2ulaw(resampled_data, 2)
                                encoded_payload = base64.b64encode(mu_law_data).decode("utf-8")

                                await websocket.send_json({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {
                                        "payload": encoded_payload,
                                    },
                                })

                            except Exception as e:
                                print(f"🔥 Audio processing error: {e}", flush=True)

                    # Tool call: send WhatsApp
                    if event_type == "response.function_call_arguments.done":
                        name = response.get("name")
                        call_id = response.get("call_id")

                        print("🛠️ TOOL CALL:", name, call_id, flush=True)

                        if name == "send_whatsapp":
                            if not whatsapp_sent_once:
                                ok = send_whatsapp_logic(phone_number)
                                whatsapp_sent_once = True
                            else:
                                ok = True

                            await xai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": "success" if ok else "failed",
                                },
                            }))

                            await xai_ws.send(json.dumps({
                                "type": "response.create"
                            }))

            async def twilio_to_xai():
                nonlocal stream_sid, phone_number

                async for message in websocket.iter_text():
                    data = json.loads(message)
                    event = data.get("event")

                    if event == "connected":
                        print("🔌 Twilio connected", flush=True)

                    elif event == "start":
                        stream_sid = data["start"]["streamSid"]
                        phone_number = data["start"]["customParameters"].get("caller")

                        print(f"📞 Call started from {phone_number} | {stream_sid}", flush=True)

                        greeting_text = (
                            "אמור בדיוק את המשפט הבא בלי לשנות כלום: "
                            "שלום, אני עוזר דיגיטלי ב-10 שקלים ליום. "
                            "תרצה שאשלח לך פרטים בוואטסאפ?"
                        )

                        greeting = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": greeting_text,
                                    }
                                ],
                            },
                        }

                        await xai_ws.send(json.dumps(greeting))
                        await xai_ws.send(json.dumps({"type": "response.create"}))

                    elif event == "media":
                        await xai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }))

                    elif event in ["stop", "close"]:
                        print("⏹️ Twilio stream stopped", flush=True)
                        break

            await asyncio.gather(
                xai_to_twilio(),
                twilio_to_xai(),
            )

    except Exception as e:
        print(f"🔥 ERROR: {e}", flush=True)

    finally:
        try:
            await websocket.close()
        except Exception:
            pass

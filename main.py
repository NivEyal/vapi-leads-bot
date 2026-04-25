import os
import json
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client

app = FastAPI()

# --- הגדרות סביבה ---
BASE_URL = os.getenv("PUBLIC_BASE_URL", "").replace("https://", "").replace("http://", "").rstrip("/")
TWILIO_CLIENT = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

def normalize_whatsapp_number(phone):
    if not phone: return ""
    phone = phone.replace(" ", "").replace("-", "")
    if phone.startswith("0"): phone = "+972" + phone[1:]
    elif phone.startswith("972"): phone = "+" + phone
    return f"whatsapp:{phone}"

def send_whatsapp_logic(to_number):
    if not to_number: return False
    formatted_number = normalize_whatsapp_number(to_number)
    try:
        TWILIO_CLIENT.messages.create(
            from_=os.getenv("TWILIO_WHATSAPP_FROM"),
            body="שלום! הנה הפרטים על העוזר הדיגיטלי ב-10 שקלים ליום. נשמח לתאם שיחה קצרה.",
            to=formatted_number
        )
        print(f"✅ WhatsApp sent to {formatted_number}", flush=True)
        return True
    except Exception as e:
        print(f"❌ WhatsApp Error: {e}", flush=True)
        return False

@app.get("/")
def home(): return {"ok": True, "service": "Grok-Twilio-Bridge"}

@app.get("/healthz")
def healthz(): return {"status": "healthy"}

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
    headers = {"Authorization": f"Bearer {os.getenv('XAI_API_KEY')}"}

    try:
        async with websockets.connect(
            xai_url, 
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20
        ) as xai_ws:
            print("✅ Connected to xAI Realtime", flush=True)

            stream_sid = None
            phone_number = None
            whatsapp_sent_once = False

            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": "אתה עוזר עסקי בעברית. דבר קצר. אם הלקוח מאשר, שלח וואטסאפ.",
                    "voice": os.getenv("GROK_VOICE", "leo"),
                    # תיקון פורמט לפי ה-Docs של xAI
                    "input_audio_format": {"type": "audio/pcmu"},
                    "output_audio_format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad", "threshold": 0.5},
                    "tools": [{
                        "type": "function",
                        "name": "send_whatsapp",
                        "description": "Send details via WhatsApp",
                        "parameters": {"type": "object", "properties": {}}
                    }]
                }
            }
            await xai_ws.send(json.dumps(session_update))

            async def xai_to_twilio():
                nonlocal stream_sid, whatsapp_sent_once
                async for message in xai_ws:
                    response = json.loads(message)
                    event_type = response.get("type")
                    
                    if event_type in ["response.audio.delta", "response.output_audio.delta"]:
                        payload = response.get("delta") or response.get("audio")
                        if payload and stream_sid:
                            # לוג לדיבוג - מוודא שזה Base64
                            print(f"AUDIO DELTA: {len(payload)} | {payload[:20]}", flush=True)
                            
                            # תיקון: שליחת ה-payload המקורי ללא str()
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": payload}
                            })

                    if event_type == "response.function_call_arguments.done":
                        call_id = response.get("call_id")
                        if response.get("name") == "send_whatsapp":
                            res = send_whatsapp_logic(phone_number) if not whatsapp_sent_once else True
                            whatsapp_sent_once = True
                            await xai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {"type": "function_call_output", "call_id": call_id, "output": "success"}
                            }))
                            await xai_ws.send(json.dumps({"type": "response.create"}))

            async def twilio_to_xai():
                nonlocal stream_sid, phone_number
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data.get('event') == 'start':
                        stream_sid = data['start']['streamSid']
                        phone_number = data['start']['customParameters'].get('caller')
                        print(f"📞 Call Started: {phone_number}", flush=True)
                        
                        greeting = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "תפתח במשפט קצר בעברית: שלום, לשלוח לך פרטים בוואטסאפ?"}]
                            }
                        }
                        await xai_ws.send(json.dumps(greeting))
                        await xai_ws.send(json.dumps({"type": "response.create"}))
                    
                    elif data.get('event') == 'media':
                        await xai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append", 
                            "audio": data['media']['payload']
                        }))
                    
                    elif data.get('event') in ['stop', 'close']:
                        break

            await asyncio.gather(xai_to_twilio(), twilio_to_xai())

    except Exception as e:
        print(f"🔥 Error: {e}", flush=True)
    finally:
        try: await websocket.close()
        except: pass

import os
import json
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client
import gspread
from google.oauth2.service_account import Credentials

app = FastAPI()

# --- הגדרות סביבה ---
BOT_LANGUAGE = os.getenv("BOT_LANGUAGE", "he").strip()
BASE_URL = os.getenv("PUBLIC_BASE_URL", "").replace("https://", "").replace("http://", "").rstrip("/")

# --- פונקציות עזר ---
def normalize_whatsapp_number(phone):
    if not phone: return ""
    phone = phone.replace(" ", "").replace("-", "")
    if phone.startswith("0"): phone = "+972" + phone[1:]
    elif phone.startswith("972"): phone = "+" + phone
    return f"whatsapp:{phone}"

TWILIO_CLIENT = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

def send_whatsapp_logic(to_number):
    if not to_number: return False
    formatted_number = normalize_whatsapp_number(to_number)
    try:
        TWILIO_CLIENT.messages.create(
            from_=os.getenv("TWILIO_WHATSAPP_FROM"),
            body="שלום! הנה הפרטים על העוזר הדיגיטלי ב-10 שקלים ליום. נשמח לתאם שיחה קצרה.",
            to=formatted_number
        )
        return True
    except: return False

@app.get("/")
def home(): return {"ok": True}

@app.get("/healthz")
def healthz(): return {"status": "healthy"}

@app.post("/voice")
async def handle_voice(request: Request):
    form = await request.form()
    caller = form.get("From", "Unknown")
    resp = VoiceResponse()
    connect = Connect()
    # הוספת תמיכה ב-track כדי לוודא שטוויליו מבין שמדובר בדו-כיווני
    stream = Stream(url=f"wss://{BASE_URL}/media-stream")
    stream.parameter(name="caller", value=caller)
    connect.append(stream)
    resp.append(connect)
    return Response(content=str(resp), media_type="application/xml")

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("🚀 WebSocket Accepted", flush=True)

    xai_url = "wss://api.x.ai/v1/realtime"
    headers = {"Authorization": f"Bearer {os.getenv('XAI_API_KEY')}"}

    try:
        async with websockets.connect(xai_url, additional_headers=headers) as xai_ws:
            stream_sid = None
            phone_number = None

            # הגדרה רזה מאוד - לפעמים יותר מדי פרמטרים גורמים לבעיות קידוד
            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": "אתה עוזר עסקי קצר. דבר עברית. אם הלקוח רוצה פרטים, שלח וואטסאפ.",
                    "voice": os.getenv("GROK_VOICE", "leo"),
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5 # החזרה ל-0.5 ובדיקת ה-Audio Buffer
                    },
                    "tools": [{
                        "type": "function",
                        "name": "send_whatsapp",
                        "description": "Send details",
                        "parameters": {"type": "object", "properties": {}}
                    }]
                }
            }
            await xai_ws.send(json.dumps(session_update))

            async def xai_to_twilio():
                nonlocal stream_sid
                async for message in xai_ws:
                    response = json.loads(message)
                    event_type = response.get("type")
                    
                    # אם יש רעש, בוא נראה אם יש שגיאות מהצד של xAI
                    if event_type == "error":
                        print(f"❌ XAI ERROR: {response}", flush=True)

                    if event_type in ["response.audio.delta", "response.output_audio.delta"]:
                        payload = response.get("delta") or response.get("audio")
                        if payload and stream_sid:
                            # וודא שהאירוע נשלח בדיוק בפורמט שטוויליו מצפה
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": str(payload)}
                            })

            async def twilio_to_xai():
                nonlocal stream_sid, phone_number
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data.get('event') == 'start':
                        stream_sid = data['start']['streamSid']
                        phone_number = data['start']['customParameters'].get('caller')
                        print(f"📞 Connected Sid: {stream_sid}", flush=True)
                        
                        # שליחת הודעת טקסט ראשונית למודל כדי שיפיק אודיו בעצמו
                        greeting = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "תפתח במשפט: שלום, לשלוח לך פרטים בוואטסאפ?"}]
                            }
                        }
                        await xai_ws.send(json.dumps(greeting))
                        await xai_ws.send(json.dumps({"type": "response.create"}))
                    
                    elif data.get('event') == 'media':
                        # שליחה ל-xAI
                        await xai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append", 
                            "audio": data['media']['payload']
                        }))
                    
                    elif data.get('event') in ['stop', 'close']: break

            await asyncio.gather(xai_to_twilio(), twilio_to_xai())

    except Exception as e: print(f"🔥 ERROR: {e}", flush=True)
    finally:
        try: await websocket.close()
        except: pass

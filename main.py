import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client
import gspread
from google.oauth2.service_account import Credentials

app = FastAPI()

# --- הגדרות סביבה ובדיקות תקינות ---
BOT_LANGUAGE = os.getenv("BOT_LANGUAGE", "he").strip()
BOT_LOCALE = os.getenv("BOT_LOCALE", "he-IL").strip()
BASE_URL = os.getenv("PUBLIC_BASE_URL", "").replace("https://", "").replace("http://", "").rstrip("/")

if not BASE_URL:
    print("⚠️ WARNING: PUBLIC_BASE_URL is not set. WebSocket connections will fail.", flush=True)

LANGUAGE_INSTRUCTIONS = {
    "he": """
חשוב מאוד:
- שפת ברירת המחדל היא עברית ישראלית.
- תדבר בעברית בלבד. אם הלקוח אומר yes/yeah/ok/sure, תבין שזה "כן".
- תשובות קצרות מאוד: משפט אחד או שניים.
""",
    "en": "Default language is English. Keep it short.",
}

# --- פונקציות עזר ונורמליזציה ---

def normalize_whatsapp_number(phone):
    if not phone: return ""
    phone = phone.replace(" ", "").replace("-", "")
    if phone.startswith("0"): phone = "+972" + phone[1:]
    elif phone.startswith("972"): phone = "+" + phone
    elif not phone.startswith("+"): phone = "+" + phone
    return f"whatsapp:{phone}"

# --- אתחול משאבים ---

def init_sheets():
    try:
        creds_info = {
            "type": os.getenv("GOOGLE_TYPE"),
            "project_id": os.getenv("GOOGLE_PROJECT_ID"),
            "private_key": os.getenv("GOOGLE_PRIVATE_KEY").replace('\\n', '\n'),
            "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
            "token_uri": os.getenv("GOOGLE_TOKEN_URI"),
        }
        creds = Credentials.from_service_account_info(creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return gspread.authorize(creds).open_by_key(os.getenv("GOOGLE_SHEETS_ID")).get_worksheet(0)
    except Exception as e:
        print(f"Sheets Init Error: {e}")
        return None

SHEET = init_sheets()
TWILIO_CLIENT = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

def send_whatsapp_logic(to_number):
    """מחזיר True אם נשלח בהצלחה"""
    if not to_number: return False
    formatted_number = normalize_whatsapp_number(to_number)
    try:
        TWILIO_CLIENT.messages.create(
            from_=os.getenv("TWILIO_WHATSAPP_FROM"),
            body="שלום! הנה הפרטים על העוזר הדיגיטלי ב-10 שקלים ליום. נשמח לתאם שיחה קצרה.",
            to=formatted_number
        )
        print(f"✅ WhatsApp sent to {formatted_number}", flush=True)
        if SHEET: SHEET.append_row([to_number, "Success"])
        return True
    except Exception as e:
        print(f"❌ WhatsApp Error: {e}", flush=True)
        return False

# --- Endpoints ל-Health Check ---

@app.get("/")
def home():
    return {
        "ok": True,
        "service": "Grok-Twilio-Bridge",
        "language": BOT_LANGUAGE
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

# --- WebSocket Main Logic ---

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("🚀 Connection accepted", flush=True)

    xai_url = "wss://api.x.ai/v1/realtime"
    headers = {"Authorization": f"Bearer {os.getenv('XAI_API_KEY')}"}

    try:
        async with websockets.connect(
            xai_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        ) as xai_ws:
            print("✅ Connected to xAI realtime", flush=True)

            stream_sid = None
            phone_number = None
            whatsapp_sent_once = False


            # הגדרת סשן מול Grok
            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": f"""
{LANGUAGE_INSTRUCTIONS.get(BOT_LANGUAGE, LANGUAGE_INSTRUCTIONS["he"])}
מטרת השיחה: להציע פרטים בוואטסאפ על עוזר דיגיטלי ב-10 שקלים ליום.
אם הלקוח אומר כן / תשלח / אוקיי:
1. הפעל את הכלי send_whatsapp (חובה להפעיל את הכלי!).
2. רק אחרי ההפעלה, אמור: "מעולה, שלחתי לך עכשיו הודעה."
אם הלקוח אומר לא: אמור "תודה ולהתראות" ואל תפעיל את הכלי.
""",
                    "voice": os.getenv("GROK_VOICE", "leo"),
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "turn_detection": {"type": "server_vad", "threshold": 0.45},
                    "tools": [{
                        "type": "function",
                        "name": "send_whatsapp",
                        "description": "שולח הודעת וואטסאפ עם פרטים ללקוח",
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
                            await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
                    
                    if event_type == "input_audio_buffer.speech_started" and stream_sid:
                        await websocket.send_json({"event": "clear", "streamSid": stream_sid})

                    if event_type == "response.function_call_arguments.done":
                        call_id = response.get("call_id")
                        if response.get("name") == "send_whatsapp":
                            ok = False
                            if not whatsapp_sent_once:
                                ok = send_whatsapp_logic(phone_number)
                                whatsapp_sent_once = True
                            else:
                                ok = True # מניעת שליחה כפולה, אך החזרת הצלחה למודל

                            await xai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {"type": "function_call_output", "call_id": call_id, "output": "success" if ok else "failed"}
                            }))
                            await xai_ws.send(json.dumps({"type": "response.create"}))

            async def twilio_to_xai():
                nonlocal stream_sid, phone_number
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    event = data.get('event')
                    
                    if event == 'start':
                        stream_sid = data['start']['streamSid']
                        phone_number = data['start']['customParameters'].get('caller')
                        print(f"📞 Started: {phone_number}", flush=True)
                        
                        greeting = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "פתח בדיוק כך: שלום, אני עוזר דיגיטלי שחוסך זמן בטלפונים ובוואטסאפ. לשלוח לך פרטים בוואטסאפ?"}]
                            }
                        }
                        await xai_ws.send(json.dumps(greeting))
                        await xai_ws.send(json.dumps({"type": "response.create"}))

                    elif event == 'media':
                        await xai_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": data['media']['payload']}))
                    
                    elif event in ['stop', 'close']:
                        print(f"⏹️ Twilio Stream {event}", flush=True)
                        break

            await asyncio.gather(xai_to_twilio(), twilio_to_xai())

    except Exception as e:
        print(f"🔥 Error: {e}", flush=True)
    finally:
        try:
            await websocket.close()
        except:
            pass

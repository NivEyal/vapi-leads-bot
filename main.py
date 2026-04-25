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

# פתרון לגרסאות פייתון 3.13+ ב-Render
try:
    import audioop
except ImportError:
    try:
        from audioop_lts import audioop
    except ImportError:
        audioop = None

app = FastAPI()

# --- הגדרות סביבה ---
BASE_URL = os.getenv("PUBLIC_BASE_URL", "").replace("https://", "").replace("http://", "").rstrip("/")

# --- פונקציות עזר (Sheets & WhatsApp) ---

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
        if SHEET: SHEET.append_row([to_number, "Success"])
        return True
    except Exception as e:
        print(f"❌ WhatsApp Error: {e}", flush=True)
        return False

# --- Endpoints ---

@app.get("/")
def home(): return {"ok": True, "service": "Grok-Twilio-Bridge-Fixed"}

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

# --- WebSocket Logic ---

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("🚀 Twilio WebSocket Connected", flush=True)

    xai_url = "wss://api.x.ai/v1/realtime"
    headers = [("Authorization", f"Bearer {os.getenv('XAI_API_KEY')}")]

    try:
        async with websockets.connect(xai_url, extra_headers=headers) as xai_ws:
            print("✅ Connected to xAI Realtime", flush=True)

            stream_sid = None
            phone_number = None
            whatsapp_sent_once = False
            resample_state = None 

            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": (
                        "אתה עוזר דיגיטלי עסקי בשם גרוק. תדבר בעברית בלבד. "
                        "תפקידך להציע את השירות של העוזר הדיגיטלי ב-10 שקלים ליום. "
                        "אם הלקוח אומר כן, אוקיי, תשלח, או yes - הפעל את הכלי send_whatsapp. "
                        "תשובות קצרות מאוד: משפט אחד."
                    ),
                    "voice": os.getenv("GROK_VOICE", "leo"),
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "pcm16", # מעבירים PCM16 לתיקון המהירות
                    "turn_detection": {
                        "type": "server_vad", 
                        "threshold": 0.4,
                        "silence_duration_ms": 1000 # מחכה שנייה של שקט כדי לזהות "כן" טוב יותר
                    },
                    "tools": [{
                        "type": "function",
                        "name": "send_whatsapp",
                        "description": "Send WhatsApp details",
                        "parameters": {"type": "object", "properties": {}}
                    }]
                }
            }
            await xai_ws.send(json.dumps(session_update))

            async def xai_to_twilio():
                nonlocal stream_sid, whatsapp_sent_once, resample_state
                async for message in xai_ws:
                    response = json.loads(message)
                    event_type = response.get("type")
                    
                    if event_type in ["response.audio.delta", "response.output_audio.delta"]:
                        payload = response.get("delta") or response.get("audio")
                        if payload and stream_sid and audioop:
                            pcm_data = base64.b64decode(payload)
                            # --- תיקון המהירות (Resampling מ-24k ל-8k) ---
                            resampled_data, resample_state = audioop.ratecv(pcm_data, 2, 1, 24000, 8000, resample_state)
                            mu_law_data = audioop.lin2ulaw(resampled_data, 2)
                            encoded_payload = base64.b64encode(mu_law_data).decode('utf-8')
                            
                            await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": encoded_payload}})
                    
                    if event_type == "response.function_call_arguments.done":
                        if response.get("name") == "send_whatsapp":
                            send_whatsapp_logic(phone_number)
                            whatsapp_sent_once = True
                            await xai_ws.send(json.dumps({"type": "response.create"}))

            async def twilio_to_xai():
                nonlocal stream_sid, phone_number
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data.get('event') == 'start':
                        stream_sid = data['start']['streamSid']
                        phone_number = data['start']['customParameters'].get('caller')
                        
                        # --- משפט הפתיחה המדויק שלך ---
                        greeting_text = "שלום, אני עוזר דיגיטלי ב-10 שקלים ליום. תרצה שאשלח לך פרטים בוואטסאפ?"
                        
                        greeting = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": f"תפתח בדיוק במשפט הזה: {greeting_text}"}]
                            }
                        }
                        await xai_ws.send(json.dumps(greeting))
                        await xai_ws.send(json.dumps({"type": "response.create"}))

                    elif data.get('event') == 'media':
                        await xai_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": data['media']['payload']}))
                    elif data.get('event') in ['stop', 'close']: break

            await asyncio.gather(xai_to_twilio(), twilio_to_xai())

    except Exception as e: print(f"🔥 Error: {e}")
    finally:
        try: await websocket.close()
        except: pass

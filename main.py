import os
import json
import asyncio
import websockets
import base64
from fastapi import FastAPI, WebSocket, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client

# פתרון לגרסאות פייתון 3.13 ומעלה שבהן audioop הוסר
try:
    import audioop
except ImportError:
    try:
        from audioop_lts import audioop
    except ImportError:
        audioop = None
        print("❌ Critical: audioop or audioop-lts not found. Audio conversion will fail.")

app = FastAPI()

# --- הגדרות סביבה ---
BASE_URL = os.getenv("PUBLIC_BASE_URL", "").replace("https://", "").replace("http://", "").rstrip("/")
TWILIO_CLIENT = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

@app.get("/")
def home(): return {"ok": True}

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

            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": "Professional business assistant. Speak Hebrew ONLY. Short responses.",
                    "voice": "leo",
                    # מבקשים PCM16 כדי שנוכל להמיר אותו ידנית ל-mulaw של טוויליו
                    "input_audio_format": "g711_ulaw", 
                    "output_audio_format": "pcm16", 
                    "turn_detection": {"type": "server_vad", "threshold": 0.5}
                }
            }
            await xai_ws.send(json.dumps(session_update))

            async def xai_to_twilio():
                nonlocal stream_sid
                async for message in xai_ws:
                    response = json.loads(message)
                    event_type = response.get("type")
                    
                    if event_type in ["response.audio.delta", "response.output_audio.delta"]:
                        payload = response.get("delta") or response.get("audio")
                        if payload and stream_sid and audioop:
                            try:
                                # 1. פענוח ה-Base64 של xAI (שמגיע כ-PCM16)
                                pcm_data = base64.b64decode(payload)
                                
                                # 2. המרה מ-PCM (16-bit linear) ל-μ-law (שנקרא mulaw בטוויליו)
                                # הפרמטר 2 מציין 2 bytes per sample (16-bit)
                                mu_law_data = audioop.lin2ulaw(pcm_data, 2) 
                                
                                # 3. קידוד חזרה ל-Base64 עבור טוויליו
                                encoded_payload = base64.b64encode(mu_law_data).decode('utf-8')

                                print("S", end="", flush=True)
                                await websocket.send_json({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": encoded_payload}
                                })
                            except Exception as e:
                                print(f"\nAudio conversion error: {e}")

            async def twilio_to_xai():
                nonlocal stream_sid, phone_number
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data.get('event') == 'start':
                        stream_sid = data['start']['streamSid']
                        phone_number = data['start']['customParameters'].get('caller')
                        print(f"📞 Connected: {phone_number}", flush=True)
                        
                        greeting = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "תגיד בעברית: שלום, אני עוזר דיגיטלי. לשלוח לך פרטים בוואטסאפ?"}]
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
        print(f"🔥 Error: {e}")
    finally:
        try:
            await websocket.close()
        except:
            pass

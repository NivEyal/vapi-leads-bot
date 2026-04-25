import os
import json
import asyncio
import websockets
import base64
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
TWILIO_CLIENT = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

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
        async with websockets.connect(xai_url, additional_headers=headers) as xai_ws:
            stream_sid = None
            phone_number = None
            resample_state = None 

            # הגדרות סשן עם הטקסט המקורי והנחיות זיהוי משופרות
            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": (
                        "אתה עוזר דיגיטלי עסקי בשם גרוק. תפקידך להציע את השירות של העוזר הדיגיטלי ב-10 שקלים ליום. "
                        "דבר בעברית טבעית וקצרה. אם הלקוח אומר כן, או מאשר, תגיד לו שאתה שולח לו את כל הפרטים לוואטסאפ ברגע זה. "
                        "אל תנתק את השיחה עד שסיימת להגיד ששלחת וואטסאפ."
                    ),
                    "voice": "leo",
                    "input_audio_format": "g711_ulaw", 
                    "output_audio_format": "pcm16", 
                    "turn_detection": {
                        "type": "server_vad", 
                        "threshold": 0.4, # הורדתי מעט כדי שיהיה רגיש יותר לדיבור חלש
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 800 # מחכה כמעט שנייה של שקט לפני שמחליט שסיימת
                    }
                }
            }
            await xai_ws.send(json.dumps(session_update))

            async def xai_to_twilio():
                nonlocal stream_sid, resample_state
                async for message in xai_ws:
                    response = json.loads(message)
                    event_type = response.get("type")
                    
                    if event_type in ["response.audio.delta", "response.output_audio.delta"]:
                        payload = response.get("delta") or response.get("audio")
                        if payload and stream_sid and audioop:
                            try:
                                pcm_data = base64.b64decode(payload)
                                # המרה מ-24k ל-8k כדי לתקן את מהירות הדיבור
                                resampled_data, resample_state = audioop.ratecv(
                                    pcm_data, 2, 1, 24000, 8000, resample_state
                                )
                                mu_law_data = audioop.lin2ulaw(resampled_data, 2)
                                encoded_payload = base64.b64encode(mu_law_data).decode('utf-8')

                                await websocket.send_json({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": encoded_payload}
                                })
                            except Exception as e:
                                print(f"Audio processing error: {e}")

            async def twilio_to_xai():
                nonlocal stream_sid, phone_number
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data.get('event') == 'start':
                        stream_sid = data['start']['streamSid']
                        phone_number = data['start']['customParameters'].get('caller')
                        
                        # המשפט המקורי שלך
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
                        await xai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append", 
                            "audio": data['media']['payload']
                        }))
                    elif data.get('event') in ['stop', 'close']: break

            await asyncio.gather(xai_to_twilio(), twilio_to_xai())

    except Exception as e: print(f"🔥 ERROR: {e}")
    finally:
        try: await websocket.close()
        except: pass

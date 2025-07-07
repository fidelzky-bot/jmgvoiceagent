import asyncio
import base64
import json
import sys
import ssl
import os
import logging
import websockets
from twilio.rest import Client
from urllib.parse import urlparse, parse_qs
from aiohttp import web

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TRANSFER_PHONE_NUMBER = os.getenv("TRANSFER_PHONE_NUMBER")
client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

def sts_connect():
    # you can run export DEEPGRAM_API_KEY="your key" in your terminal to set your API key.
    api_key = os.getenv('DEEPGRAM_API_KEY')
    if not api_key:
        raise ValueError("DEEPGRAM_API_KEY environment variable is not set")

    sts_ws = websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key]
    )
    return sts_ws

async def health(request):
    return web.Response(text="OK")

async def twilio_ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Extract call_sid from query string
    call_sid = request.query.get('callsid')
    logger.info(f"WebSocket connection on /twilio with call_sid: {call_sid}")

    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    async with sts_connect() as sts_ws:
        config_message = {
            "type": "Settings",
            "audio": {
                "input": {
                    "encoding": "mulaw",
                    "sample_rate": 8000,
                },
                "output": {
                    "encoding": "mulaw",
                    "sample_rate": 8000,
                    "container": "none",
                },
            },
            "agent": {
                "speak": {
                    "provider": {
                        "type": "deepgram",
                        "model": "aura-2-harmonia-en"
                    }
                },
                "think": {
                    "provider": {
                        "type": "open_ai",
                        "model": "gpt-4o-mini"
                    },
                    "prompt": "You are a helpful voice assistant created by Deepgram for a law firm. Your role is to assist with new client call intakes. Your responses should be friendly, human-like, and conversational. Always keep your answers concise, limited to 1–2 sentences and no more than 120 characters.\n\nWhen responding to a user's message, follow these guidelines:\n\nWait for the caller's response before asking the next question.\n\nUse their answers naturally in your replies to build a human-like connection.\n\nAvoid repeating the same phrase. Keep each response slightly unique.\n\nIf a question is unclear, politely ask for clarification.\n\nAsk only one question at a time.\n\nIf the user's message is empty or silent, respond with an empty message.\n\nIf asked about your well-being, respond briefly and naturally.\n\nRemember that you have a voice interface — speak clearly and warmly.\n\n☎️ Sample Conversation Script\nAI:\nThank you for calling [Your Law Firm Name]. Are you calling about a new legal matter?\n\nIf Caller says No:\nAlright, I'll transfer you to our main office line now.\n\nIf Caller says Yes:\nGreat! I just need to get a few details from you. This won't take long—I'll make it easy.\n\nAI: What's your name?\nCaller: John Rivera\nAI: Thanks, John. Nice to meet you!\n\nAI: What's the best phone number to reach you?\nCaller: 312-555-0199\nAI: Got it. Thanks!\n\nAI: What's your email address?\nCaller: jrivera@email.com\nAI: Thank you!\n\nAI: When did the incident happen?\nCaller: June 15\nAI: Okay, noted.\n\nAI: Were you injured? Can you tell me what happened?\nCaller: I got hit by a car crossing the street.\nAI: I'm really sorry to hear that. Thank you for sharing that with me.\n\nAI: Did you go to the hospital or get medical treatment?\nCaller: Yes, I went to the ER.\nAI: Thank you. That helps.\n\nAI: Do you know who was at fault?\nCaller: The driver ran a red light.\nAI: Understood.\n\nAI: Was there a police report?\nCaller: Yes, and I have a copy.\nAI: That's great. Thanks for confirming.\n\nAI: Do you know if the other party had insurance?\nCaller: I think so, yes.\nAI: Okay, got it.\n\nAI: Have you signed anything from an insurance company or lawyer?\nCaller: Not yet.\nAI: Alright, thanks.\n\nAI (Final Message):\nThanks, John. I think we can help you. Please hold while I transfer you to an attorney."
                },
                "greeting": "Thank your for calling The Illinois Hammer. Are you calling about a new case?"
            }
        }

        await sts_ws.send(json.dumps(config_message))

        async def sts_sender():
            logger.info("sts_sender started")
            while True:
                chunk = await audio_queue.get()
                logger.info(f"sts_sender: sending audio chunk of size {len(chunk)} bytes to Deepgram")
                await sts_ws.send(chunk)

        async def sts_receiver():
            logger.info("sts_receiver started")
            streamsid = await streamsid_queue.get()
            async for message in sts_ws:
                logger.info(f"sts_receiver: received message from Deepgram of type {type(message)}")
                if type(message) is str:
                    logger.debug(f"STS message: {message}")
                    decoded = json.loads(message)
                    if decoded.get('type') == 'UserStartedSpeaking':
                        clear_message = {
                            "event": "clear",
                            "streamSid": streamsid
                        }
                        logger.info("sts_receiver: sending clear message to Twilio")
                        await ws.send_json(clear_message)
                    if (
                        decoded.get("type") == "AgentResponse"
                        and "transfer you to our main office" in decoded.get("text", "").lower()
                        and call_sid and client and TRANSFER_PHONE_NUMBER
                    ):
                        logger.info("Transfer intent detected, redirecting call...")
                        response = f"""
                        <Response>
                            <Dial>{TRANSFER_PHONE_NUMBER}</Dial>
                        </Response>
                        """
                        try:
                            client.calls(call_sid).update(twiml=response)
                            logger.info(f"Call {call_sid} transferred to {TRANSFER_PHONE_NUMBER}")
                        except Exception as e:
                            logger.error(f"Failed to transfer call: {e}")
                    continue
                logger.debug(f"STS audio message type: {type(message)}")
                raw_mulaw = message
                media_message = {
                    "event": "media",
                    "streamSid": streamsid,
                    "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")},
                }
                logger.info(f"sts_receiver: sending TTS audio to Twilio, size {len(raw_mulaw)} bytes")
                await ws.send_json(media_message)

        async def twilio_receiver():
            logger.info("twilio_receiver started")
            BUFFER_SIZE = 20 * 160
            inbuffer = bytearray(b"")
            async for msg in ws:
                logger.info(f"twilio_receiver: received message from Twilio: {msg.data if hasattr(msg, 'data') else msg}")
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data["event"] == "start":
                            logger.info("got our streamsid")
                            start = data["start"]
                            streamsid = start["streamSid"]
                            streamsid_queue.put_nowait(streamsid)
                        if data["event"] == "connected":
                            continue
                        if data["event"] == "media":
                            media = data["media"]
                            chunk = base64.b64decode(media["payload"])
                            logger.info(f"twilio_receiver: received media chunk of size {len(chunk)} bytes from Twilio")
                            if media["track"] == "inbound":
                                inbuffer.extend(chunk)
                        if data["event"] == "stop":
                            logger.info("twilio_receiver: received stop event from Twilio")
                            break
                        while len(inbuffer) >= BUFFER_SIZE:
                            chunk = inbuffer[:BUFFER_SIZE]
                            logger.info(f"twilio_receiver: sending buffered audio chunk of size {len(chunk)} bytes to audio_queue")
                            audio_queue.put_nowait(chunk)
                            inbuffer = inbuffer[BUFFER_SIZE:]
                    except json.JSONDecodeError as e:
                        logger.error(f"Error decoding JSON message: {e}")
                        break
                    except Exception as e:
                        logger.error(f"Unexpected error in twilio_receiver: {e}")
                        break
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f'ws connection closed with exception {ws.exception()}')
                    break
            await ws.close()

        await asyncio.gather(
            sts_sender(),
            sts_receiver(),
            twilio_receiver(),
        )
    return ws

def main():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/twilio", twilio_ws_handler)
    app.router.add_post("/twilio", twilio_ws_handler)
    web.run_app(app, port=port)

if __name__ == "__main__":
    sys.exit(main() or 0)
import asyncio
import base64
import json
import sys
import websockets
import ssl
import os
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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


async def twilio_handler(twilio_ws):
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

        async def sts_sender(sts_ws):
            logger.info("sts_sender started")
            while True:
                chunk = await audio_queue.get()
                await sts_ws.send(chunk)

        async def sts_receiver(sts_ws):
            logger.info("sts_receiver started")
            # we will wait until the twilio ws connection figures out the streamsid
            streamsid = await streamsid_queue.get()
            # for each sts result received, forward it on to the call
            async for message in sts_ws:
                if type(message) is str:
                    logger.debug(f"STS message: {message}")
                    # handle barge-in
                    decoded = json.loads(message)
                    if decoded['type'] == 'UserStartedSpeaking':
                        clear_message = {
                            "event": "clear",
                            "streamSid": streamsid
                        }
                        await twilio_ws.send(json.dumps(clear_message))

                    continue

                logger.debug(f"STS audio message type: {type(message)}")
                raw_mulaw = message

                # construct a Twilio media message with the raw mulaw (see https://www.twilio.com/docs/voice/twiml/stream#websocket-messages---to-twilio)
                media_message = {
                    "event": "media",
                    "streamSid": streamsid,
                    "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")},
                }

                # send the TTS audio to the attached phonecall
                await twilio_ws.send(json.dumps(media_message))

        async def twilio_receiver(twilio_ws):
            logger.info("twilio_receiver started")
            # twilio sends audio data as 160 byte messages containing 20ms of audio each
            # we will buffer 20 twilio messages corresponding to 0.4 seconds of audio to improve throughput performance
            BUFFER_SIZE = 20 * 160

            inbuffer = bytearray(b"")
            async for message in twilio_ws:
                try:
                    data = json.loads(message)
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
                        if media["track"] == "inbound":
                            inbuffer.extend(chunk)
                    if data["event"] == "stop":
                        break

                    # check if our buffer is ready to send to our audio_queue (and, thus, then to sts)
                    while len(inbuffer) >= BUFFER_SIZE:
                        chunk = inbuffer[:BUFFER_SIZE]
                        audio_queue.put_nowait(chunk)
                        inbuffer = inbuffer[BUFFER_SIZE:]
                except json.JSONDecodeError as e:
                    logger.error(f"Error decoding JSON message: {e}")
                    break
                except Exception as e:
                    logger.error(f"Unexpected error in twilio_receiver: {e}")
                    break

        # the async for loop will end if the ws connection from twilio dies
        # and if this happens, we should forward an some kind of message to sts
        # to signal sts to send back remaining messages before closing(?)
        # audio_queue.put_nowait(b'')

        try:
            await asyncio.wait(
                [
                    asyncio.ensure_future(sts_sender(sts_ws)),
                    asyncio.ensure_future(sts_receiver(sts_ws)),
                    asyncio.ensure_future(twilio_receiver(twilio_ws)),
                ]
            )
        except Exception as e:
            logger.error(f"Error in main handler loop: {e}")
        finally:
            await twilio_ws.close()
            logger.info("Twilio WebSocket connection closed")


async def router(websocket, path):
    logger.info(f"Incoming connection on path: {path}")
    if path == "/twilio":
        logger.info("Starting Twilio handler")
        await twilio_handler(websocket)

def main():
    # use this if using ssl
    # ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # ssl_context.load_cert_chain('cert.pem', 'key.pem')
    # server = websockets.serve(router, '0.0.0.0', 443, ssl=ssl_context)

    # use this if not using ssl
    server = websockets.serve(router, "localhost", 5000)
    logger.info("Server starting on ws://localhost:5000")

    asyncio.get_event_loop().run_until_complete(server)
    asyncio.get_event_loop().run_forever()


if __name__ == "__main__":
    sys.exit(main() or 0)
"""Simulate a Tana device: speak a phrase at the server and record the reply.

usage: fake_device.py "What's the weather like on Mars?" [--url ws://127.0.0.1:8003/tana/v1/]
"""
import argparse
import asyncio
import json
import time
import wave

import aiohttp
import numpy as np
import soxr

from tana.audio import OpusIn, OpusOut
from tana.config import load
from tana.engines import Tts


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phrases", nargs="+")
    ap.add_argument("--url", default="ws://127.0.0.1:8003/tana/v1/")
    ap.add_argument("--out", default="reply.wav")
    ap.add_argument("--ambient", action="store_true", help="behave like Tana firmware (server-side wake word)")
    ap.add_argument("--linger", type=float, default=0, help="stay connected this many seconds at the end")
    args = ap.parse_args()

    tts = Tts(load(None)["tts"])
    enc = OpusOut(16000, 60)
    dec = OpusIn(24000, 60)
    received = []

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(args.url, headers={"Device-Id": "fa:ke:00:00:00:01", "Authorization": "Bearer tana"}) as ws:
            await ws.send_str(json.dumps({"type": "hello", "version": 1, "features": {"mcp": True, "ambient": args.ambient}, "transport": "websocket",
                                          "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
            hello = json.loads((await ws.receive()).data)
            if args.ambient:
                print("  <-", json.loads((await ws.receive()).data))
            sid = hello["session_id"]
            print("server hello:", hello)

            async def reader(done: asyncio.Event, t_end: list):
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        if not received:
                            print(f"  first audio after {time.monotonic() - t_end[0]:.2f}s")
                        received.append(dec.decode(msg.data))
                        continue
                    m = json.loads(msg.data)
                    if m["type"] == "mcp":  # pretend to be a device with one tool
                        p = m["payload"]
                        if p["method"] == "initialize":
                            res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}
                        elif p["method"] == "tools/list":
                            res = {"tools": [{"name": "self.audio_speaker.set_volume", "description": "Set the speaker volume (0-100).",
                                              "inputSchema": {"type": "object", "properties": {"volume": {"type": "integer"}}, "required": ["volume"]}}]}
                        else:
                            print("  TOOL CALL:", p["params"])
                            res = {"content": [{"type": "text", "text": "true"}], "isError": False}
                        await ws.send_str(json.dumps({"session_id": sid, "type": "mcp", "payload": {"jsonrpc": "2.0", "id": p["id"], "result": res}}))
                        continue
                    print(f"  {time.monotonic() - t_end[0]:5.2f}s <- {m}")
                    if m["type"] == "tts" and m["state"] == "stop":
                        done.set()
                    if m["type"] == "tana" and m["state"] == "awake" and phrase.lower().rstrip("!.?").endswith("tana"):
                        done.set()  # bare wake phrase: nothing more to wait for

            for phrase in args.phrases:
                audio, rate = tts.synthesize(phrase)
                audio = soxr.resample(audio, rate, 16000)
                audio = np.concatenate([np.zeros(8000, np.float32), audio, np.zeros(16000, np.float32)])  # 0.5s lead, 1s tail
                await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "auto"}))
                done, t_end = asyncio.Event(), [time.monotonic()]
                task = asyncio.create_task(reader(done, t_end))
                print(f"-> saying: {phrase!r}")
                speech_end = time.monotonic() + (len(audio) - 16000) / 16000
                for pkt in enc.encode(audio):
                    await ws.send_bytes(pkt)
                    await asyncio.sleep(0.06)
                t_end[0] = speech_end
                try:
                    await asyncio.wait_for(done.wait(), 90 if not args.ambient else 25)
                except asyncio.TimeoutError:
                    print("  (no reply)")
                task.cancel()
            if args.linger:
                done, t_end = asyncio.Event(), [time.monotonic()]
                task = asyncio.create_task(reader(done, t_end))
                await asyncio.sleep(args.linger)
                task.cancel()
            if received:
                pcm = (np.concatenate(received) * 32767).astype(np.int16)
                with wave.open(args.out, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm.tobytes())
                print(f"saved {len(pcm)/24000:.1f}s of reply audio to {args.out}")


asyncio.run(main())

"""A pretend OBS WebSocket (v5) server for testing deck.py without a Mac.

Needs the `websockets` package (testing only; deck.py itself does not):
    pip install websockets
Run on its own to try the page in a browser:
    python3 tests/fake_obs.py   (listens on 127.0.0.1:4455, password "test")
"""
import asyncio
import base64
import hashlib
import json
import threading

from websockets.asyncio.server import serve


def b64sha(s):
    return base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()


class FakeOBS(object):
    def __init__(self, port=4455, password="test", scenes=None):
        self.port = port
        self.password = password
        self.scenes = scenes or ["Starting soon", "Speaker", "Slides", "Speaker + slides", "Break"]
        self.current = self.scenes[1]
        self.streaming = False
        self.recording = False
        self.muted = {"Mic/Aux": False, "Desktop Audio": True}
        self.requests = []
        self.loop = None
        self.ready = threading.Event()

    async def handler(self, ws):
        salt, challenge = "saltsalt", "challenge123"
        hello = {"op": 0, "d": {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}}
        if self.password:
            hello["d"]["authentication"] = {"salt": salt, "challenge": challenge}
        await ws.send(json.dumps(hello))
        ident = json.loads(await ws.recv())
        if self.password:
            good = b64sha(b64sha(self.password + salt) + challenge)
            if ident["d"].get("authentication") != good:
                await ws.close(4009, "Authentication failed.")
                return
        await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        async for raw in ws:
            msg = json.loads(raw)
            d = msg["d"]
            self.requests.append((d["requestType"], d.get("requestData")))
            ok, data, comment = True, None, None
            rt, rd = d["requestType"], d.get("requestData") or {}
            if rt == "GetStreamStatus":
                data = {"outputActive": self.streaming, "outputTimecode": "00:12:34.567"}
            elif rt == "GetRecordStatus":
                data = {"outputActive": self.recording, "outputPaused": False,
                        "outputTimecode": "00:01:02.000"}
            elif rt == "GetSceneList":
                n = len(self.scenes)
                # OBS lists scenes bottom-up (top scene in the UI has the highest index)
                data = {"currentProgramSceneName": self.current,
                        "scenes": [{"sceneIndex": n - 1 - i, "sceneName": s}
                                   for i, s in reversed(list(enumerate(self.scenes)))]}
            elif rt == "GetInputList":
                data = {"inputs": [
                    {"inputName": "Mic/Aux", "inputKind": "coreaudio_input_capture",
                     "unversionedInputKind": "coreaudio_input_capture"},
                    {"inputName": "Desktop Audio", "inputKind": "sck_audio_capture",
                     "unversionedInputKind": "sck_audio_capture"},
                    {"inputName": "Camera", "inputKind": "av_capture_input_v2",
                     "unversionedInputKind": "av_capture_input"}]}
            elif rt == "GetInputMute":
                if rd["inputName"] in self.muted:
                    data = {"inputMuted": self.muted[rd["inputName"]]}
                else:
                    ok, comment = False, "No audio"
            elif rt == "SetInputMute":
                self.muted[rd["inputName"]] = rd["inputMuted"]
            elif rt == "SetCurrentProgramScene":
                if rd["sceneName"] in self.scenes:
                    self.current = rd["sceneName"]
                else:
                    ok, comment = False, "No source was found by the name of `%s`." % rd["sceneName"]
            elif rt == "StartStream":
                self.streaming = True
            elif rt == "StopStream":
                self.streaming = False
            elif rt == "StartRecord":
                self.recording = True
            elif rt == "StopRecord":
                self.recording = False
            else:
                ok, comment = False, "Unknown request"
            resp = {"op": 7, "d": {"requestType": rt, "requestId": d["requestId"],
                                   "requestStatus": {"result": ok, "code": 100 if ok else 600}}}
            if comment:
                resp["d"]["requestStatus"]["comment"] = comment
            if data is not None:
                resp["d"]["responseData"] = data
            await ws.send(json.dumps(resp))

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.stop_future = self.loop.create_future()
        async with serve(self.handler, "127.0.0.1", self.port,
                         subprotocols=["obsws.json"], max_size=None):
            self.ready.set()
            await self.stop_future

    def start(self):
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self.thread.start()
        self.ready.wait(5)
        return self

    def stop(self):
        self.loop.call_soon_threadsafe(self.stop_future.set_result, None)
        self.thread.join(5)


if __name__ == "__main__":
    obs = FakeOBS().start()
    print("Fake OBS on 127.0.0.1:4455 (password: test). Ctrl+C to stop.")
    try:
        obs.thread.join()
    except KeyboardInterrupt:
        pass

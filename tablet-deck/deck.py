#!/usr/bin/env python3
"""Mizan Deck: big OBS and Zoom buttons on a tablet, served from a Mac.

Runs on the Mac with nothing extra installed (Python standard library only):

    python3 deck.py

Then open the address it prints on the tablet's browser. The page is plain
HTML with old-style JavaScript so it works on old Android browsers.

OBS is controlled through its built-in WebSocket server (OBS 28 or newer:
Tools > WebSocket Server Settings). Zoom is controlled by sending its own
keyboard shortcuts to the Zoom app, which needs Accessibility permission.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import random
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("DECK_CONFIG", os.path.join(HERE, "config.json"))
DRY_RUN = os.environ.get("DECK_DRY_RUN") == "1" or sys.platform != "darwin"

DEFAULTS = {
    "port": 8765,
    "pin": "",
    "obs_host": "127.0.0.1",
    "obs_port": 4455,
    "obs_password": "",
    # Leave empty to show every audio source OBS has; or list names, e.g. ["Mic/Aux"]
    "audio_sources": [],
    # After sending a shortcut to Zoom, switch back to the app you were using
    "zoom_return_to_previous_app": True,
}

# Zoom for Mac keyboard shortcuts. Each one is a toggle in Zoom itself.
ZOOM_BUTTONS = [
    ("mic", "Mute / unmute me", "a", "cmd,shift"),
    ("video", "Camera on / off", "v", "cmd,shift"),
    ("share", "Share screen / stop", "s", "cmd,shift"),
    ("muteall", "Mute everyone", "m", "cmd,ctrl"),
    ("chat", "Show / hide chat", "h", "cmd,shift"),
    ("people", "Participants", "u", "cmd"),
    ("record", "Record to cloud", "c", "cmd,shift"),
    ("pause", "Pause / resume recording", "p", "cmd,shift"),
]
ZOOM_BY_ID = dict((b[0], b) for b in ZOOM_BUTTONS)

# Audio input kinds on macOS (and others, so the same file works anywhere)
AUDIO_KINDS = ("coreaudio_input_capture", "coreaudio_output_capture",
               "sck_audio_capture", "wasapi_input_capture",
               "wasapi_output_capture", "pulse_input_capture",
               "pulse_output_capture")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if not str(cfg.get("pin") or "").strip():
        # Never run without a PIN: make one and save it.
        cfg["pin"] = "%04d" % random.SystemRandom().randint(0, 9999)
        save = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                save = json.load(f)
        save["pin"] = cfg["pin"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(save, f, indent=2)
            f.write("\n")
    cfg["pin"] = str(cfg["pin"]).strip()
    return cfg


# --------------------------------------------------------------------------
# OBS WebSocket (protocol v5) client, standard library only
# --------------------------------------------------------------------------

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class OBSError(Exception):
    """Something went wrong talking to OBS. The message is shown to the user."""


class OBSRequestError(OBSError):
    """OBS understood the request but refused it."""


class OBSClosed(OBSError):
    def __init__(self, code, reason):
        OBSError.__init__(self, "OBS closed the connection (%s %s)" % (code, reason))
        self.code = code
        self.reason = reason


def _b64sha256(text):
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def friendly_obs_error(err):
    if isinstance(err, ConnectionRefusedError):
        return ("Can't reach OBS. Is OBS open, with its WebSocket server "
                "turned on (Tools > WebSocket Server Settings)?")
    if isinstance(err, OBSClosed) and err.code == 4009:
        return "OBS rejected the password. Check obs_password in config.json."
    if isinstance(err, socket.timeout):
        return "OBS didn't answer in time."
    if isinstance(err, OBSError):
        return str(err)
    return "Lost contact with OBS (%s)." % err.__class__.__name__


class OBSClient(object):
    def __init__(self, host, port, password, timeout=3.0):
        self.host = host
        self.port = int(port)
        self.password = password or ""
        self.timeout = timeout
        self.sock = None
        self._buf = b""
        self.lock = threading.Lock()

    # -- WebSocket framing --------------------------------------------------

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OBSError("OBS closed the connection.")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _recv_frame(self):
        b1, b2 = self._recv_exact(2)
        fin, opcode = b1 & 0x80, b1 & 0x0F
        masked, length = b2 & 0x80, b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + body)

    def _send_json(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode("utf-8"))

    def _recv_json(self):
        parts = []
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x9:            # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:            # pong
                continue
            if opcode == 0x8:            # close
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                raise OBSClosed(code, payload[2:].decode("utf-8", "replace"))
            parts.append(payload)
            if fin:
                return json.loads(b"".join(parts).decode("utf-8"))

    # -- Connection ---------------------------------------------------------

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._buf = b""

    def _connect(self):
        self._close()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock = s
        key = base64.b64encode(os.urandom(16))
        s.sendall((
            "GET / HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: obsws.json\r\n\r\n"
            % (self.host, self.port, key.decode("ascii"))).encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise OBSError("OBS closed the connection while connecting.")
            resp += chunk
            if len(resp) > 65536:
                raise OBSError("Unexpected reply from the OBS port.")
        head, _, rest = resp.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status = lines[0].split()
        if len(status) < 2 or status[1] != b"101":
            raise OBSError("Something other than OBS is answering on port %d." % self.port)
        expected = base64.b64encode(hashlib.sha1(key + WS_GUID).digest())
        accept = b""
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-accept":
                accept = value.strip()
        if accept != expected:
            raise OBSError("Something other than OBS is answering on port %d." % self.port)
        self._buf = rest

        hello = self._recv_json()
        if hello.get("op") != 0:
            raise OBSError("Unexpected first message from OBS.")
        identify = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = (hello.get("d") or {}).get("authentication")
        if auth:
            if not self.password:
                raise OBSError("OBS wants a password. Put it in config.json as obs_password.")
            secret = _b64sha256(self.password + auth["salt"])
            identify["authentication"] = _b64sha256(secret + auth["challenge"])
        self._send_json({"op": 1, "d": identify})
        ready = self._recv_json()
        if ready.get("op") != 2:
            raise OBSError("OBS didn't accept the login.")

    def request(self, request_type, data=None):
        """Send one request and return OBS's responseData (a dict)."""
        with self.lock:
            for attempt in (1, 2):
                had_connection = self.sock is not None
                try:
                    if self.sock is None:
                        self._connect()
                    rid = uuid.uuid4().hex
                    msg = {"op": 6, "d": {"requestType": request_type, "requestId": rid}}
                    if data:
                        msg["d"]["requestData"] = data
                    self._send_json(msg)
                    while True:
                        resp = self._recv_json()
                        d = resp.get("d") or {}
                        if resp.get("op") == 7 and d.get("requestId") == rid:
                            break
                    status = d.get("requestStatus") or {}
                    if not status.get("result"):
                        raise OBSRequestError(status.get("comment")
                                              or "OBS refused (code %s)." % status.get("code"))
                    return d.get("responseData") or {}
                except OBSRequestError:
                    raise
                except (OSError, OBSError, ValueError) as err:
                    self._close()
                    # A connection that was fine before may just have gone stale: retry once.
                    if attempt == 1 and had_connection:
                        continue
                    raise OBSError(friendly_obs_error(err))


def obs_status(obs, cfg):
    st = {"connected": False, "error": None, "scenes": [], "current": None,
          "streaming": False, "stream_time": "", "recording": False,
          "record_paused": False, "record_time": "", "audio": []}
    try:
        stream = obs.request("GetStreamStatus")
        record = obs.request("GetRecordStatus")
        scenes = obs.request("GetSceneList")
        st["streaming"] = bool(stream.get("outputActive"))
        st["stream_time"] = (stream.get("outputTimecode") or "").split(".")[0]
        st["recording"] = bool(record.get("outputActive"))
        st["record_paused"] = bool(record.get("outputPaused"))
        st["record_time"] = (record.get("outputTimecode") or "").split(".")[0]
        # OBS lists scenes bottom-up; show them in the same order as the OBS window.
        ordered = sorted(scenes.get("scenes") or [],
                         key=lambda s: s.get("sceneIndex", 0), reverse=True)
        st["scenes"] = [s.get("sceneName") for s in ordered]
        st["current"] = scenes.get("currentProgramSceneName")

        wanted = list(cfg.get("audio_sources") or [])
        if not wanted:
            for inp in obs.request("GetInputList").get("inputs") or []:
                kind = inp.get("unversionedInputKind") or inp.get("inputKind") or ""
                if kind in AUDIO_KINDS:
                    wanted.append(inp.get("inputName"))
        for name in wanted[:6]:
            try:
                muted = obs.request("GetInputMute", {"inputName": name}).get("inputMuted")
            except OBSRequestError:
                continue
            st["audio"].append({"name": name, "muted": bool(muted)})
        st["connected"] = True
    except OBSError as err:
        st["error"] = str(err)
    return st


def obs_action(obs, action, value):
    if action == "scene":
        obs.request("SetCurrentProgramScene", {"sceneName": value})
        return "Switched to %s" % value
    if action == "stream":
        if value == "start":
            obs.request("StartStream")
            return "Starting the livestream"
        obs.request("StopStream")
        return "Livestream ended"
    if action == "record":
        if value == "start":
            obs.request("StartRecord")
            return "Recording started"
        obs.request("StopRecord")
        return "Recording stopped and saved"
    if action in ("mute", "unmute"):
        obs.request("SetInputMute", {"inputName": value, "inputMuted": action == "mute"})
        return "%s %s" % (value, "muted" if action == "mute" else "is live")
    raise OBSError("Unknown button.")


# --------------------------------------------------------------------------
# Zoom (keyboard shortcuts via AppleScript)
# --------------------------------------------------------------------------

ZOOM_SCRIPT = r'''
on run argv
    set theKey to item 1 of argv
    set mods to item 2 of argv
    set goBack to item 3 of argv
    tell application "System Events"
        if not (exists process "zoom.us") then return "NOZOOM"
        set prev to first application process whose frontmost is true
        set frontmost of process "zoom.us" to true
    end tell
    delay 0.3
    tell application "System Events"
        if mods is "cmd,shift" then
            keystroke theKey using {command down, shift down}
        else if mods is "cmd,ctrl" then
            keystroke theKey using {command down, control down}
        else
            keystroke theKey using {command down}
        end if
    end tell
    if goBack is "1" then
        delay 0.2
        tell application "System Events" to set frontmost of prev to true
    end if
    return "OK"
end run
'''

dry_run_log = []


def zoom_action(button_id, cfg):
    if button_id not in ZOOM_BY_ID:
        return False, "Unknown Zoom button."
    _, label, key, mods = ZOOM_BY_ID[button_id]
    go_back = "1" if cfg.get("zoom_return_to_previous_app", True) else "0"
    if DRY_RUN:
        dry_run_log.append((key, mods))
        return True, "Zoom: %s (test mode, nothing sent)" % label
    try:
        res = subprocess.run(["osascript", "-e", ZOOM_SCRIPT, key, mods, go_back],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as err:
        return False, "Couldn't reach Zoom (%s)." % err.__class__.__name__
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    if out == "NOZOOM":
        return False, "Zoom isn't open on the Mac."
    if res.returncode != 0:
        if "1002" in err or "1719" in err or "assistive" in err.lower() or "not allowed" in err.lower():
            return False, ("The Mac blocked the keypress. Allow Terminal in System Settings > "
                           "Privacy & Security > Accessibility, then try again.")
        return False, "Zoom didn't take the shortcut: %s" % (err[:160] or "unknown error")
    return True, "Zoom: %s" % label


# --------------------------------------------------------------------------
# Web page (plain HTML + ES3-style JavaScript for old Android browsers)
# --------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s), quote=True)


CSS = """
*{box-sizing:border-box;-webkit-box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:#10151a;color:#e8edf1;font-family:Helvetica,Arial,sans-serif;
  -webkit-tap-highlight-color:rgba(0,0,0,0);-webkit-user-select:none}
.top{padding:10px 16px;background:#0b0f13;border-bottom:1px solid #26313b;overflow:hidden}
.top b{font-size:20px;letter-spacing:.5px}
.top .r{float:right;font-size:14px;color:#9fb0bf;padding-top:4px}
.col{float:left;width:60%;padding:6px 8px 16px 16px}
.col.zoom{width:40%;padding:6px 16px 16px 8px}
@media (max-width:820px){.col,.col.zoom{float:none;width:100%;padding:6px 12px}}
h2{font-size:15px;text-transform:uppercase;letter-spacing:2px;color:#7f93a5;margin:10px 4px 6px}
h3{font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#5f7385;margin:12px 4px 4px}
.grid{overflow:hidden}
form{display:inline;margin:0}
.btn{display:inline-block;vertical-align:top;margin:4px;border:0;border-radius:12px;
  background:#1f2a35;color:#e8edf1;font-size:19px;font-weight:bold;line-height:1.2;
  height:86px;padding:6px 8px;border-bottom:4px solid #141c24;font-family:inherit}
.g3 .btn{width:31%}
.g2 .btn{width:47%}
.btn small{display:block;font-size:12px;font-weight:normal;color:#9fb0bf;margin-top:4px}
.btn:active{background:#2f3f4f;border-bottom-width:1px;margin-top:7px;height:83px}
.btn.on{background:#1d6b4f;border-bottom-color:#0f3d2d}
.btn.on small{color:#bfe8d6}
.btn.go{background:#1d6b4f;border-bottom-color:#0f3d2d}
.btn.stop{background:#a3262f;border-bottom-color:#5e1419}
.btn.muted{background:#7a2a2f;border-bottom-color:#4a171a}
.btn.muted small{color:#f2c6c9}
.pills{margin:4px}
.pill{display:inline-block;padding:6px 12px;border-radius:20px;background:#1f2a35;
  font-size:15px;font-weight:bold;margin:0 6px 4px 0;color:#9fb0bf}
.pill.live{background:#c62832;color:#fff}
.pill.rec{background:#c62832;color:#fff}
.warn{background:#3a2a10;color:#ffd58a;border-radius:10px;padding:14px;margin:6px 4px;font-size:16px;line-height:1.4}
.note{color:#6f8394;font-size:12px;margin:8px 4px;line-height:1.4}
#toast{position:fixed;left:16px;right:16px;bottom:16px;padding:14px 16px;border-radius:10px;
  font-size:17px;font-weight:bold;background:#1d6b4f;color:#fff;display:none;text-align:center}
#toast.bad{background:#a3262f}
#lost{display:none;background:#a3262f;color:#fff;padding:8px 16px;font-weight:bold;text-align:center}
.login{max-width:360px;margin:60px auto;text-align:center;padding:0 16px}
.login input{font-size:34px;width:100%;padding:12px;border-radius:10px;border:0;text-align:center;
  letter-spacing:10px;margin:16px 0}
.login .btn{width:100%;margin:0}
"""

JS = r"""
var busy = false, polling = false, toastTimer = null;
function el(id) { return document.getElementById(id); }
function toast(msg, ok) {
  var t = el('toast');
  t.innerHTML = '';
  t.appendChild(document.createTextNode(msg));
  t.className = ok ? '' : 'bad';
  t.style.display = 'block';
  if (toastTimer) { clearTimeout(toastTimer); }
  toastTimer = setTimeout(function () { t.style.display = 'none'; }, ok ? 2200 : 6000);
}
function formBody(form) {
  var parts = [], i, e;
  for (i = 0; i < form.elements.length; i++) {
    e = form.elements[i];
    if (e.name) { parts.push(encodeURIComponent(e.name) + '=' + encodeURIComponent(e.value)); }
  }
  return parts.join('&');
}
function send(form) {
  var ask = form.getAttribute('data-confirm'), x;
  if (ask && !window.confirm(ask)) { return false; }
  if (busy) { return false; }
  try { x = new XMLHttpRequest(); } catch (err) { return true; }
  busy = true;
  x.open('POST', '/do', true);
  x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
  x.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
  x.onreadystatechange = function () {
    var r;
    if (x.readyState !== 4) { return; }
    busy = false;
    try { r = JSON.parse(x.responseText); } catch (err) { r = { ok: false, message: 'No answer from the Mac.' }; }
    if (x.status === 403) { window.location.href = '/'; return; }
    toast(r.message, r.ok);
    refresh();
  };
  x.send(formBody(form));
  return false;
}
function refresh() {
  var x;
  if (polling) { return; }
  polling = true;
  try { x = new XMLHttpRequest(); } catch (err) { polling = false; return; }
  x.open('GET', '/panel?_=' + (new Date()).getTime(), true);
  x.onreadystatechange = function () {
    if (x.readyState !== 4) { return; }
    polling = false;
    if (x.status === 200) {
      el('panel').innerHTML = x.responseText;
      el('lost').style.display = 'none';
    } else if (x.status === 403) {
      window.location.href = '/';
    } else {
      el('lost').style.display = 'block';
    }
  };
  x.send(null);
}
setInterval(refresh, 2000);
toastTimer = setTimeout(function () { el('toast').style.display = 'none'; }, 4000);
"""


def page(title, body, script=""):
    return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no\">"
            "<meta name=\"apple-mobile-web-app-capable\" content=\"yes\">"
            "<meta name=\"mobile-web-app-capable\" content=\"yes\">"
            "<title>%s</title><style>%s</style></head><body>%s%s</body></html>"
            % (esc(title), CSS, body, ("<script>%s</script>" % script) if script else ""))


def button(token, action, value, label, cls="", sub="", confirm=""):
    return ('<form method="post" action="/do" onsubmit="return send(this)"%s>'
            '<input type="hidden" name="t" value="%s">'
            '<input type="hidden" name="action" value="%s">'
            '<input type="hidden" name="value" value="%s">'
            '<button type="submit" class="btn %s">%s%s</button></form>'
            % ((' data-confirm="%s"' % esc(confirm)) if confirm else "",
               esc(token), esc(action), esc(value), esc(cls), esc(label),
               ("<small>%s</small>" % esc(sub)) if sub else ""))


def render_panel(st, token):
    out = ['<div class="col obs"><h2>OBS</h2>']
    if not st["connected"]:
        out.append('<div class="warn">%s</div>' % esc(st["error"]))
        out.append('<div class="note">The buttons come back by themselves once OBS is reachable.</div>')
    else:
        out.append('<div class="pills">')
        if st["streaming"]:
            out.append('<span class="pill live">&#9679; LIVE %s</span>' % esc(st["stream_time"]))
        else:
            out.append('<span class="pill">Not live</span>')
        if st["recording"]:
            out.append('<span class="pill rec">&#9679; REC %s%s</span>'
                       % (esc(st["record_time"]), " (paused)" if st["record_paused"] else ""))
        else:
            out.append('<span class="pill">Not recording</span>')
        out.append('</div>')

        out.append('<h3>Scenes</h3><div class="grid g3">')
        for name in st["scenes"]:
            on = name == st["current"]
            out.append(button(token, "scene", name, name, "on" if on else "",
                              "On air" if on else ""))
        out.append('</div>')

        out.append('<h3>Stream &amp; record</h3><div class="grid g3">')
        if st["streaming"]:
            out.append(button(token, "stream", "stop", "End stream", "stop",
                              st["stream_time"], "End the livestream now?"))
        else:
            out.append(button(token, "stream", "start", "Go live", "go",
                              "Start streaming", "Start the livestream now?"))
        if st["recording"]:
            out.append(button(token, "record", "stop", "Stop recording", "stop",
                              st["record_time"], "Stop and save the recording?"))
        else:
            out.append(button(token, "record", "start", "Record", "", "Start recording"))
        out.append('</div>')

        if st["audio"]:
            out.append('<h3>Audio</h3><div class="grid g3">')
            for a in st["audio"]:
                if a["muted"]:
                    out.append(button(token, "unmute", a["name"], a["name"], "muted",
                                      "MUTED - tap to unmute"))
                else:
                    out.append(button(token, "mute", a["name"], a["name"], "on",
                                      "Live - tap to mute"))
            out.append('</div>')
    out.append('</div>')

    out.append('<div class="col zoom"><h2>Zoom</h2><div class="grid g2">')
    for bid, label, _, _ in ZOOM_BUTTONS:
        confirm = "Mute everyone in the meeting?" if bid == "muteall" else ""
        out.append(button(token, "zoom", bid, label, confirm=confirm))
    out.append('</div><div class="note">Zoom buttons work while you are in a meeting. '
               'They flip Zoom\'s own settings, so check the Mac if unsure which way it went.</div></div>')
    return "".join(out)


def render_main(st, token, msg="", ok=True):
    body = ('<div class="top"><span class="r">Mizan Institute</span><b>Mizan Deck</b></div>'
            '<div id="lost">Lost connection to the Mac. Is the deck still running?</div>'
            '<div id="panel">%s</div><div id="toast"%s>%s</div>'
            % (render_panel(st, token),
               (' style="display:block"' + ('' if ok else ' class="bad"')) if msg else "",
               esc(msg)))
    return page("Mizan Deck", body, JS)


def render_login(msg=""):
    body = ('<div class="login"><h2>Mizan Deck</h2>'
            '<form method="post" action="/login">'
            '<input type="password" name="pin" inputmode="numeric" pattern="[0-9]*" '
            'placeholder="PIN" autofocus>'
            '<button type="submit" class="btn go">Unlock</button></form>%s</div>'
            % (('<div class="warn">%s</div>' % esc(msg)) if msg else ""))
    return page("Mizan Deck", body)


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Deck(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.obs = OBSClient(cfg["obs_host"], cfg["obs_port"], cfg["obs_password"])
        self.token = hashlib.sha256(("mizan-deck:" + cfg["pin"]).encode("utf-8")).hexdigest()
        self.failed = []          # timestamps of wrong PIN attempts
        self.fail_lock = threading.Lock()

    def locked_out(self):
        with self.fail_lock:
            now = time.time()
            self.failed = [t for t in self.failed if now - t < 60]
            return len(self.failed) >= 5

    def run_action(self, action, value):
        if action == "zoom":
            return zoom_action(value, self.cfg)
        try:
            return True, obs_action(self.obs, action, value)
        except OBSError as err:
            return False, str(err)


def make_handler(deck):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MizanDeck/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, where, headers=None):
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            for k, v in (headers or []):
                self.send_header(k, v)
            self.end_headers()

        def _authed(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "deck" and hmac.compare_digest(value, deck.token):
                    return True
            return False

        def _form(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(n, 10000)).decode("utf-8", "replace")
            return dict((k, v[0]) for k, v in parse_qs(raw).items())

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                if not self._authed():
                    return self._send(200, render_login())
                q = parse_qs(url.query)
                msg = (q.get("msg") or [""])[0]
                ok = (q.get("ok") or ["1"])[0] == "1"
                st = obs_status(deck.obs, deck.cfg)
                return self._send(200, render_main(st, deck.token, msg, ok))
            if url.path == "/panel":
                if not self._authed():
                    return self._send(403, "locked", "text/plain")
                st = obs_status(deck.obs, deck.cfg)
                return self._send(200, render_panel(st, deck.token))
            self._send(404, "Not found", "text/plain")

        def do_POST(self):
            url = urlparse(self.path)
            form = self._form()
            if url.path == "/login":
                if deck.locked_out():
                    return self._send(200, render_login("Too many wrong tries. Wait a minute."))
                if hmac.compare_digest(form.get("pin", "").strip(), deck.cfg["pin"]):
                    cookie = "deck=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict" % deck.token
                    return self._redirect("/", [("Set-Cookie", cookie)])
                with deck.fail_lock:
                    deck.failed.append(time.time())
                return self._send(200, render_login("Wrong PIN."))
            if url.path == "/do":
                xhr = self.headers.get("X-Requested-With") == "XMLHttpRequest"
                if not (self._authed() and hmac.compare_digest(form.get("t", ""), deck.token)):
                    if xhr:
                        return self._send(403, json.dumps({"ok": False, "message": "Locked"}),
                                          "application/json")
                    return self._redirect("/")
                ok, message = deck.run_action(form.get("action", ""), form.get("value", ""))
                print("[%s] %s %s -> %s" % (time.strftime("%H:%M:%S"), form.get("action"),
                                             form.get("value"), message))
                sys.stdout.flush()
                if xhr:
                    return self._send(200, json.dumps({"ok": ok, "message": message}),
                                      "application/json")
                return self._redirect("/?ok=%d&msg=%s" % (1 if ok else 0, quote(message)))
            self._send(404, "Not found", "text/plain")

    return Handler


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def make_server(cfg, host="0.0.0.0"):
    deck = Deck(cfg)
    server = ThreadingHTTPServer((host, int(cfg["port"])), make_handler(deck))
    server.daemon_threads = True
    server.deck = deck
    return server


def main():
    cfg = load_config()
    server = make_server(cfg)
    print("")
    print("  Mizan Deck is running.")
    print("")
    print("  On the tablet, open:   http://%s:%s" % (local_ip(), cfg["port"]))
    print("  PIN:                   %s" % cfg["pin"])
    print("")
    st = obs_status(server.deck.obs, cfg)
    print("  OBS: %s" % ("connected" if st["connected"] else st["error"]))
    if DRY_RUN:
        print("  Zoom: test mode (not a Mac), shortcuts are only logged")
    print("")
    print("  Keep this window open. Press Ctrl+C to stop.")
    print("")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()

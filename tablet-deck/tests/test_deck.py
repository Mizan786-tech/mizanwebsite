"""Tests for deck.py against a pretend OBS.  Run:  python3 -m unittest discover tests"""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ["DECK_DRY_RUN"] = "1"

import deck  # noqa: E402
from fake_obs import FakeOBS  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class OBSClientTests(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.fake = FakeOBS(self.port, "test").start()

    def tearDown(self):
        self.fake.stop()

    def test_status_and_actions(self):
        obs = deck.OBSClient("127.0.0.1", self.port, "test")
        st = deck.obs_status(obs, dict(deck.DEFAULTS))
        self.assertTrue(st["connected"], st["error"])
        self.assertEqual(st["scenes"], self.fake.scenes)       # same order as OBS window
        self.assertEqual(st["current"], "Speaker")
        self.assertEqual(st["stream_time"], "00:12:34")
        self.assertEqual([a["name"] for a in st["audio"]], ["Mic/Aux", "Desktop Audio"])

        deck.obs_action(obs, "scene", "Slides")
        deck.obs_action(obs, "stream", "start")
        deck.obs_action(obs, "record", "start")
        deck.obs_action(obs, "mute", "Mic/Aux")
        self.assertEqual(self.fake.current, "Slides")
        self.assertTrue(self.fake.streaming and self.fake.recording)
        self.assertTrue(self.fake.muted["Mic/Aux"])
        deck.obs_action(obs, "stream", "stop")
        self.assertFalse(self.fake.streaming)

    def test_refused_request_message(self):
        obs = deck.OBSClient("127.0.0.1", self.port, "test")
        with self.assertRaises(deck.OBSRequestError) as cm:
            deck.obs_action(obs, "scene", "Nope")
        self.assertIn("Nope", str(cm.exception))

    def test_wrong_password(self):
        obs = deck.OBSClient("127.0.0.1", self.port, "wrong")
        st = deck.obs_status(obs, dict(deck.DEFAULTS))
        self.assertFalse(st["connected"])
        self.assertIn("password", st["error"])

    def test_missing_password(self):
        obs = deck.OBSClient("127.0.0.1", self.port, "")
        st = deck.obs_status(obs, dict(deck.DEFAULTS))
        self.assertIn("obs_password", st["error"])

    def test_big_messages(self):
        # Long scene lists need the 16-bit and 64-bit WebSocket length forms.
        self.fake.scenes = ["Scene number %d with a long name" % i for i in range(3000)]
        self.fake.current = self.fake.scenes[0]
        obs = deck.OBSClient("127.0.0.1", self.port, "test")
        st = deck.obs_status(obs, dict(deck.DEFAULTS))
        self.assertEqual(len(st["scenes"]), 3000)
        long_name = "y" * 70000
        with self.assertRaises(deck.OBSRequestError):
            deck.obs_action(obs, "scene", long_name)   # big outgoing frame still round-trips

    def test_reconnects_after_obs_restart(self):
        obs = deck.OBSClient("127.0.0.1", self.port, "test")
        self.assertTrue(deck.obs_status(obs, dict(deck.DEFAULTS))["connected"])
        self.fake.stop()
        self.assertFalse(deck.obs_status(obs, dict(deck.DEFAULTS))["connected"])
        self.fake = FakeOBS(self.port, "test").start()
        self.assertTrue(deck.obs_status(obs, dict(deck.DEFAULTS))["connected"])


class OBSOffTests(unittest.TestCase):
    def test_obs_not_running(self):
        obs = deck.OBSClient("127.0.0.1", free_port(), "")
        st = deck.obs_status(obs, dict(deck.DEFAULTS))
        self.assertFalse(st["connected"])
        self.assertIn("Is OBS open", st["error"])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.obs_port = free_port()
        self.fake = FakeOBS(self.obs_port, "test").start()
        cfg = dict(deck.DEFAULTS, port=free_port(), pin="2468",
                   obs_port=self.obs_port, obs_password="test")
        self.server = deck.make_server(cfg, host="127.0.0.1")
        self.port = cfg["port"]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.token = self.server.deck.token
        deck.dry_run_log[:] = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fake.stop()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if body is not None:
            body = urlencode(body)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body, h)
        r = c.getresponse()
        return r, r.read().decode()

    def cookie(self):
        return {"Cookie": "deck=" + self.token}

    def test_locked_without_pin(self):
        r, body = self.req("GET", "/")
        self.assertIn('name="pin"', body)
        self.assertNotIn("Speaker", body)
        r, body = self.req("GET", "/panel")
        self.assertEqual(r.status, 403)

    def test_wrong_then_right_pin(self):
        r, body = self.req("POST", "/login", {"pin": "0000"})
        self.assertIn("Wrong PIN", body)
        r, body = self.req("POST", "/login", {"pin": "2468"})
        self.assertEqual(r.status, 303)
        self.assertIn("deck=" + self.token, r.getheader("Set-Cookie"))

    def test_lockout(self):
        for _ in range(5):
            self.req("POST", "/login", {"pin": "0000"})
        r, body = self.req("POST", "/login", {"pin": "2468"})
        self.assertIn("Too many", body)

    def test_page_shows_obs_and_zoom(self):
        r, body = self.req("GET", "/", headers=self.cookie())
        for text in ["Starting soon", "Speaker + slides", "Go live", "Mic/Aux",
                     "MUTED", "Mute / unmute me", "Share screen"]:
            self.assertIn(text, body)

    def test_action_needs_form_token(self):
        h = dict(self.cookie(), **{"X-Requested-With": "XMLHttpRequest"})
        r, body = self.req("POST", "/do", {"action": "stream", "value": "start"}, h)
        self.assertEqual(r.status, 403)
        self.assertFalse(self.fake.streaming)

    def test_xhr_scene_switch(self):
        h = dict(self.cookie(), **{"X-Requested-With": "XMLHttpRequest"})
        r, body = self.req("POST", "/do", {"t": self.token, "action": "scene", "value": "Break"}, h)
        self.assertEqual(json.loads(body), {"ok": True, "message": "Switched to Break"})
        self.assertEqual(self.fake.current, "Break")
        r, body = self.req("GET", "/panel", headers=self.cookie())
        self.assertIn("On air", body)

    def test_no_javascript_fallback(self):
        r, body = self.req("POST", "/do", {"t": self.token, "action": "record", "value": "start"},
                           self.cookie())
        self.assertEqual(r.status, 303)
        self.assertTrue(r.getheader("Location").startswith("/?ok=1&msg="))
        self.assertTrue(self.fake.recording)

    def test_zoom_button(self):
        h = dict(self.cookie(), **{"X-Requested-With": "XMLHttpRequest"})
        r, body = self.req("POST", "/do", {"t": self.token, "action": "zoom", "value": "mic"}, h)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(deck.dry_run_log, [("a", "cmd,shift")])

    def test_scene_names_are_escaped(self):
        self.fake.scenes.append('<script>alert(1)</script>')
        r, body = self.req("GET", "/panel", headers=self.cookie())
        self.assertNotIn("<script>alert", body)


class ConfigTests(unittest.TestCase):
    def test_pin_is_created_and_saved(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        with open(path, "w") as f:
            json.dump({"obs_password": "abc"}, f)
        cfg = deck.load_config(path)
        self.assertRegex(cfg["pin"], r"^\d{4}$")
        with open(path) as f:
            saved = json.load(f)
        self.assertEqual(saved, {"obs_password": "abc", "pin": cfg["pin"]})


if __name__ == "__main__":
    unittest.main()

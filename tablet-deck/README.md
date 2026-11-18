# Mizan Deck

Turns the old Samsung Galaxy Tab (or any phone, tablet or laptop on the same
Wi-Fi) into a button panel for **OBS** and **Zoom** on the Mac. It works like a
Stream Deck.

- **OBS:** a button for every scene, Go live / End stream, Record / Stop, and mute
  buttons for each audio source. The buttons show what's live right now.
- **Zoom:** mute/unmute me, camera, share screen, mute everyone, chat,
  participants, record to cloud, pause recording.

Nothing extra needs installing. It only uses what comes with the Mac.

## One-time setup on the Mac

1. **Copy this `tablet-deck` folder** onto the Mac, e.g. into Documents.
2. **Turn on OBS remote control:** in OBS, open *Tools → WebSocket Server Settings*,
   tick *Enable WebSocket server*, click *Show Connect Info* and copy the
   **Server Password**.
3. **Open `config.json`** in TextEdit and paste that password between the quotes
   after `"obs_password"`. You can also choose a PIN (e.g. `"pin": "4821"`).
   If you leave the PIN empty, one is made up for you and shown when the deck starts.
4. **Start it:** double-click **Start Deck.command**.
   - If the Mac says it's from an unidentified developer: right-click it → *Open* → *Open*.
   - If the Mac offers to install the "command line developer tools", click *Install*,
     wait, then double-click Start Deck again.
   - If asked whether Python may *accept incoming network connections*, click *Allow*
     (that's how the tablet reaches it).
5. **Allow the Zoom buttons:** the first time you press a Zoom button, the Mac asks
   to let Terminal control your computer. Allow it, or turn on **Terminal** in
   *System Settings → Privacy & Security → Accessibility*. Then press the button again.

## On the tablet

1. Connect it to the **same Wi-Fi** as the Mac.
2. Open the browser and type the address the Mac window shows,
   e.g. `http://192.168.1.23:8765`.
3. Enter the PIN. The tablet remembers it.
4. Optional: add it to the home screen as a shortcut, and set the screen timeout
   to its longest setting in *Settings → Display*.

Keep the Terminal window on the Mac open while you use the deck. Close it, or
press Ctrl+C, to stop.

## Good to know

- **Go live, End stream and Stop recording** ask "are you sure?" first, so a stray tap
  won't end a livestream.
- **Zoom buttons** press Zoom's own keyboard shortcuts. Zoom has to be in a meeting,
  and the Mac briefly flicks to Zoom and back. Each button flips Zoom's setting, so if
  in doubt, glance at the Mac. "Mute everyone" may show a confirm box on the Mac.
- **Only show some audio sources:** in `config.json` list them, e.g.
  `"audio_sources": ["Mic/Aux"]`.
- **If the Mac's address changes** (after a router restart), the deck window shows the
  new one. Giving the Mac a fixed address in the router settings stops this.
- **Security:** the deck only works on your own Wi-Fi and needs the PIN. Don't set up
  port forwarding to it on the router.

## For developers

`deck.py` is a single file using only the Python standard library. It includes its own
small OBS WebSocket v5 client. The page uses only ES3-era JavaScript and plain CSS so
Android 3/4 browsers can run it. With JavaScript off, the buttons still work as forms.

Tests run against a pretend OBS and need `pip install websockets`:

    python3 -m unittest discover tests

`python3 tests/fake_obs.py` starts the pretend OBS (password `test`) so you can try
the page on a computer without OBS.

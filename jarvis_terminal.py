"""
J.A.R.V.I.S  —  WEESTREAM INTELLIGENCE TERMINAL  v5.0
Simplified terminal  +  opens jarvis_visual.html for Siri/JARVIS animation
"""

import os, sys, time, json, base64, threading, queue, webbrowser, subprocess, math, random

def auto_install(pkg, imp=None):
    import importlib
    try: importlib.import_module(imp or pkg)
    except ImportError:
        print(f"  Installing {pkg}..."); subprocess.check_call([sys.executable,"-m","pip","install",pkg,"-q","--break-system-packages"])

auto_install("pyaudio"); auto_install("websocket-client","websocket")
auto_install("numpy");   auto_install("rich")
auto_install("SpeechRecognition","speech_recognition")

import numpy as np, pyaudio, websocket
from rich.live  import Live
from rich.text  import Text

# ── CONFIG ────────────────────────────────────────────────────────────────────
WS_URL     = "wss://prepodapi.toingg.com/api/v3/media/streaming"
TOKEN      = "psdTo0QFiErjJ9SlAG0VhI4RkKq7nTNOhmClw0xc6B5Vl5z7heCvZ6C9eW01KGgM"
CAMP_ID    = "69d79c72b7ab98a9ef49bcad"
SAMPLE_IN  = 8000
SAMPLE_OUT = 22050
CHUNK_OUT  = 2048
HTTP_PORT  = 8766
W          = 68           # display width
BARS       = W - 4        # spectrum bar count

# ── MIC SENSITIVITY CONFIG ────────────────────────────────────────────────────
# Raise MIC_ENERGY_THRESHOLD if background noise triggers responses (noisy room).
# Lower it if mic isn't picking up speech (quiet room / distant mic).
# Range: 100 (very sensitive) → 4000 (very noise-resistant). Default: 600
MIC_ENERGY_THRESHOLD   = 600

# How long (seconds) of silence before a phrase is considered complete.
# Raise if words are being cut off mid-sentence. Default: 1.2
MIC_PAUSE_THRESHOLD    = 1.2

# Calibration duration (seconds) — reads ambient noise at startup each loop.
# Raise in noisy environments so it better ignores background. Default: 0.5
MIC_CALIBRATION_TIME   = 0.5

# Barge-in sensitivity while AI is speaking.
# Raise if AI audio is triggering its own barge-in (echo). Default: 0.4
BARGE_IN_PAUSE         = 0.4

# ── STATE ─────────────────────────────────────────────────────────────────────
ws_conn      = None
pa           = pyaudio.PyAudio()
is_talking   = False
is_playing   = False
audio_queue  = queue.Queue()
stop_event   = threading.Event()
chat_lines   = []
status_msg   = "INITIALIZING SYSTEMS..."
ws_status    = "OFFLINE"
mic_rms      = 0.0
audio_rms    = 0.0
last_text    = ""
pending_urls  = []          # URLs waiting for audio to start before opening
_drop_audio   = False       # True after barge-in — drops incoming chunks until user finishes

# visual readouts (animated)
_freq_val  = 440.0
_amp_val   = 0.0
_neural    = 98.4

# Spectrum
bar_h      = [0.0] * BARS
frame_n    = 0

# ── SHARED STATE (read by HTTP server) ───────────────────────────────────────
http_state = {"state": "idle", "text": "", "status": "INITIALIZING..."}

def update_http_state():
    s = "listening" if is_talking else "speaking" if is_playing else "idle"
    http_state["state"]  = s
    http_state["text"]   = last_text
    http_state["status"] = status_msg

def log_chat(role, text):
    global last_text
    chat_lines.append((role, str(text)[:160]))
    if len(chat_lines) > 30: chat_lines.pop(0)
    if role in ("ai","user"): last_text = str(text)[:100]

def set_status(m): global status_msg; status_msg = m
def set_ws(s):     global ws_status;  ws_status  = s

# ── SPECTRUM ──────────────────────────────────────────────────────────────────
_BAR_FILLS = [" ","▁","▂","▃","▄","▅","▆","▇","█"]

def render_spectrum_block(heights, rows=7, width=76):
    lines = []
    for row in range(rows - 1, -1, -1):
        threshold = row / rows
        line = Text()
        for h in heights[:width]:
            fill = (h - threshold) * rows
            ch   = _BAR_FILLS[max(0, min(8, int(fill * 8)))] if fill > 0 else " "
            if h > 0.88 and row >= rows - 1:   c = "bold bright_white"
            elif h > 0.72 and row >= rows - 2: c = "bright_cyan"
            elif h > 0.52 and row >= rows - 3: c = "cyan"
            elif row >= rows - 5:              c = "blue"
            else:                              c = "bright_black"
            line.append(ch, style=c)
        lines.append(line)
    return lines

def animate_bars():
    global bar_h, frame_n, _freq_val, _amp_val, _neural
    t = time.time(); frame_n += 1

    if is_talking:
        _freq_val = 200 + mic_rms * 3000 + math.sin(t * 7) * 60
        _amp_val  = mic_rms
        energy    = max(mic_rms * 10, 0.35)
        for i in range(len(bar_h)):
            p  = i / len(bar_h)
            w  = math.sin(t * 9  + p * math.pi * 4) * 0.35
            w += math.sin(t * 6  + p * math.pi * 7) * 0.25
            w += math.sin(t * 14 + p * math.pi * 2) * 0.20
            w += math.sin(t * 3  + p * math.pi * 9) * 0.15
            bar_h[i] = bar_h[i] * 0.5 + max(0.05, min(1.0, (w + 1) / 2 * energy)) * 0.5
    elif is_playing:
        _freq_val = 800 + audio_rms * 5000 + math.sin(t * 11) * 150
        _amp_val  = audio_rms
        energy    = max(audio_rms * 8, 0.3)
        for i in range(len(bar_h)):
            p  = i / len(bar_h)
            w  = math.sin(t * 14 + p * math.pi * 6) * 0.40
            w += math.sin(t * 8  + p * math.pi * 3) * 0.35
            w += math.sin(t * 22 + p * math.pi * 9) * 0.15
            spike = (random.random() ** 3) * 0.30
            bar_h[i] = bar_h[i] * 0.4 + max(0.05, min(1.0, abs(w) * energy + spike)) * 0.6
    else:
        _freq_val = 220 + math.sin(t * 0.7) * 25
        _amp_val  = 0.001 + abs(math.sin(t * 0.4)) * 0.003
        for i in range(len(bar_h)):
            p = i / len(bar_h)
            h = 0.02 + ((math.sin(t * 0.9 + p * math.pi * 2) + 1) / 2 * 0.5 +
                        (math.sin(t * 2.3 + p * math.pi * 5) + 1) / 2 * 0.3) * 0.06
            bar_h[i] = bar_h[i] * 0.93 + h * 0.07
    _neural = 95 + math.sin(t * 0.3) * 3

# ── DISPLAY ───────────────────────────────────────────────────────────────────
SEP = "  " + "─" * (W - 2)

def make_display():
    animate_bars()
    update_http_state()
    t   = time.time()
    IW  = W - 2          # inner width (after left pad)
    out = Text()

    # ─── HEADER ───
    ts     = time.strftime("%H:%M:%S")
    ws_col = {"CONNECTED":"bright_green","OFFLINE":"bright_black",
              "ERROR":"bright_red","CONNECTING":"yellow"}.get(ws_status,"white")
    hdr = Text()
    hdr.append("  ")
    hdr.append("◈ J.A.R.V.I.S", style="bold bright_cyan")
    hdr.append("  /  WEESTREAM  /  ", style="bright_black")
    hdr.append(f"◈ {ws_status}", style=f"bold {ws_col}")
    hdr.append(f"   {ts}", style="bright_black")
    hdr.append(" " * max(IW - len(hdr.plain) + 2, 0))
    out.append_text(hdr)
    out.append("\n")

    out.append(SEP + "\n", style="bright_blue")

    # ─── SPECTRUM HEADER ───
    spec_hdr = Text()
    spec_hdr.append("  ")
    spec_hdr.append("▸ SPECTRAL ", style="bright_black")
    spec_hdr.append(f"FREQ {int(_freq_val):5d}Hz", style="cyan")
    spec_hdr.append("  ·  ", style="bright_black")
    spec_hdr.append(f"AMP {_amp_val:.5f}", style="blue")
    spec_hdr.append("  ·  ", style="bright_black")
    spec_hdr.append(f"NEURAL {_neural:.1f}%", style="bright_blue")
    out.append_text(spec_hdr)
    out.append("\n")

    # ─── SPECTRUM BARS ───
    sw    = IW - 1
    specs = render_spectrum_block(bar_h, rows=5, width=sw)
    for sl in specs:
        row = Text()
        row.append("  ")
        row.append_text(sl)
        out.append_text(row)
        out.append("\n")

    # ─── SCAN BEAM ───
    beam_pos = int((t * 35) % sw)
    sr = Text()
    sr.append("  ")
    for i in range(sw):
        d = abs(i - beam_pos)
        if d == 0:    sr.append("╪", style="bold bright_white")
        elif d <= 2:  sr.append("━", style="bright_cyan")
        elif d <= 5:  sr.append("─", style="cyan")
        elif d <= 12: sr.append("·", style="blue")
        else:         sr.append(" ")
    out.append_text(sr)
    out.append("\n")

    out.append(SEP + "\n", style="bright_blue")

    # ─── CHAT ───
    recent = chat_lines[-6:]
    for role, text in recent:
        if role == "ai":
            label = "JARVIS"; lc = "bright_cyan"; tc = "cyan"
        elif role == "user":
            label = "  YOU "; lc = "bright_green"; tc = "green"
        else:
            label = "  SYS "; lc = "bright_black"; tc = "bright_black"
        words = text.split()
        cur = ""; first = True
        for w in words:
            if len(cur) + len(w) + 1 > IW - 12:
                row = Text()
                row.append("  ")
                if first:
                    row.append(f"{label}", style=f"bold {lc}")
                    row.append(" ▸ ", style="bright_black")
                    first = False
                else:
                    row.append("         ", style="bright_black")
                row.append(cur, style=tc)
                out.append_text(row); out.append("\n")
                cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            row = Text()
            row.append("  ")
            if first:
                row.append(f"{label}", style=f"bold {lc}")
                row.append(" ▸ ", style="bright_black")
            else:
                row.append("         ", style="bright_black")
            row.append(cur, style=tc)
            out.append_text(row); out.append("\n")

    out.append(SEP + "\n", style="bright_blue")

    # ─── STATUS ───
    st_col = "bold bright_red" if is_talking else "bold bright_cyan" if is_playing else "bold bright_blue"
    cursor = "█" if int(t * 2) % 2 == 0 else " "
    st = Text()
    st.append("  ")
    st.append("▶ ", style=st_col)
    st.append(status_msg, style=st_col)
    st.append(cursor, style=st_col)
    out.append_text(st); out.append("\n")

    out.append(SEP + "\n", style="bright_blue")

    # ─── FOOTER ───
    lv_w    = 18
    lv_fill = min(lv_w, int(mic_rms * lv_w * 14) if is_talking else
                        int(audio_rms * lv_w * 8) if is_playing else 0)
    bar_col = "bright_red" if is_talking else "bright_cyan"
    ft = Text()
    ft.append("  ")
    ft.append("[ENTER]", style="bold yellow")
    ft.append(" mic  ", style="bright_black")
    ft.append("[Ctrl+C]", style="bold yellow")
    ft.append(" exit  ", style="bright_black")
    ft.append("◈  LEVEL ▐", style="bright_black")
    ft.append("█" * lv_fill,        style=bar_col)
    ft.append("░" * (lv_w - lv_fill), style="bright_black")
    ft.append("▌  ", style="bright_black")
    ft.append("[browser]", style="bold bright_black")
    ft.append(" visual", style="bright_black")
    out.append_text(ft); out.append("\n")

    return out

# ── CHROME HELPERS (cross-platform) ──────────────────────────────────────────
import platform as _platform

def find_chrome():
    plat = _platform.system()
    user = os.environ.get("USERNAME") or os.environ.get("USER", "")
    if plat == "Windows":
        for c in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            rf"C:\Users\{user}\AppData\Local\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]:
            if os.path.exists(c): return c
    elif plat == "Darwin":
        for c in [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]:
            if os.path.exists(c): return c
    else:
        for cmd in ["google-chrome","google-chrome-stable","chromium-browser","chromium","microsoft-edge"]:
            try:
                r = subprocess.run(["which", cmd], capture_output=True, text=True)
                if r.returncode == 0: return r.stdout.strip()
            except Exception: pass
    return None

# ── SCREEN SIZE ────────────────────────────────────────────────────────────────
def get_screen_size():
    """Return (width, height) of primary monitor."""
    try:
        if _platform.system() == "Windows":
            import ctypes
            u = ctypes.windll.user32
            return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        elif _platform.system() == "Darwin":
            out = subprocess.check_output(
                ["system_profiler", "SPDisplaysDataType"], text=True, stderr=subprocess.DEVNULL)
            import re
            m = re.search(r"Resolution: (\d+) x (\d+)", out)
            if m: return int(m.group(1)), int(m.group(2))
        else:
            out = subprocess.check_output(
                ["xrandr","--current"], text=True, stderr=subprocess.DEVNULL)
            import re
            m = re.search(r"current (\d+) x (\d+)", out)
            if m: return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1920, 1080   # safe fallback

# ── WINDOW SLOT MANAGER ────────────────────────────────────────────────────────
# 4 slots arranged in a 2x2 grid inset from screen edges
URL_WIN_W  = 860
URL_WIN_H  = 580
_PADDING   = 20          # gap from screen edge and between windows
_url_slot      = 0       # next slot (cycles 0→3)
_url_slot_wins = {}      # slot → subprocess.Popen
_slot_profiles = {}      # slot → temp profile dir path (persisted so Chrome reuses it)

import tempfile

def _ensure_profile(slot):
    """Return a stable temp profile dir for this slot (created once, reused)."""
    if slot not in _slot_profiles:
        d = os.path.join(tempfile.gettempdir(), f"jarvis_chrome_slot_{slot}")
        os.makedirs(d, exist_ok=True)
        _slot_profiles[slot] = d
    return _slot_profiles[slot]

def _slot_pos(slot):
    """
    2×2 grid of windows, inset from edges:
      slot 0 = top-left    slot 1 = top-right
      slot 2 = bottom-left slot 3 = bottom-right
    """
    sw, sh = get_screen_size()
    p = _PADDING
    col = slot % 2
    row = slot // 2
    x = p + col * (URL_WIN_W + p)
    y = p + row * (URL_WIN_H + p)
    # clamp so nothing hangs off screen
    x = min(x, sw - URL_WIN_W - p)
    y = min(y, sh - URL_WIN_H - p)
    return max(0, x), max(0, y)

def open_url_in_slot(url, slot):
    """
    Open url in slot, replacing whatever was there.
    Uses a per-slot --user-data-dir so Chrome spawns an independent
    process — bypassing single-instance restrictions and honouring
    --window-size / --window-position reliably.
    """
    global _url_slot_wins
    chrome = find_chrome()
    x, y   = _slot_pos(slot)
    profile = _ensure_profile(slot)

    # Kill previous window in this slot
    old = _url_slot_wins.get(slot)
    if old:
        try: old.terminate()
        except Exception: pass
        time.sleep(0.2)
        _url_slot_wins[slot] = None

    if chrome:
        proc = subprocess.Popen([
            chrome,
            f"--user-data-dir={profile}",   # independent instance per slot
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={URL_WIN_W},{URL_WIN_H}",
            f"--window-position={x},{y}",
            url,
        ])
        _url_slot_wins[slot] = proc
    else:
        webbrowser.open_new(url)

def close_all_url_windows():
    global _url_slot_wins
    for proc in _url_slot_wins.values():
        if proc:
            try: proc.terminate()
            except Exception: pass
    _url_slot_wins = {}

def open_visual_window():
    """Open the JARVIS visual centered on screen using its own profile."""
    url     = f"http://localhost:{HTTP_PORT}/"
    chrome  = find_chrome()
    sw, sh  = get_screen_size()
    win_w, win_h = 400, 500
    vx = (sw - win_w) // 2
    vy = (sh - win_h) // 2
    profile = os.path.join(tempfile.gettempdir(), "jarvis_chrome_visual")
    os.makedirs(profile, exist_ok=True)
    if chrome:
        subprocess.Popen([
            chrome,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={win_w},{win_h}",
            f"--window-position={vx},{vy}",
        ])
    else:
        webbrowser.open(url)

def open_new_window(url):
    """Open url in the next corner slot, cycling 0→1→2→3→0."""
    global _url_slot
    open_url_in_slot(url, _url_slot)
    _url_slot = (_url_slot + 1) % 4

# ── LOCAL HTTP SERVER (serves state to jarvis_visual.html) ────────────────────
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_visual.html")

def start_http_server():
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/state":
                data = json.dumps(http_state).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif self.path in ("/", "/index.html"):
                try:
                    with open(_HTML_PATH, "rb") as f: data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_response(404); self.end_headers()
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, *_): pass

    srv = HTTPServer(("localhost", HTTP_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

# ── AUDIO ─────────────────────────────────────────────────────────────────────
def playback_worker():
    global is_playing, audio_rms, pending_urls
    out = pa.open(format=pyaudio.paFloat32, channels=1,
                  rate=SAMPLE_OUT, output=True, frames_per_buffer=CHUNK_OUT)
    was_playing = False
    while not stop_event.is_set():
        try:
            chunk = audio_queue.get(timeout=0.2)
            # Open any pending URLs as soon as we have audio (flush once per batch)
            if pending_urls:
                urls_to_open = pending_urls[:]
                pending_urls.clear()
                def _open_on_audio_start(ul):
                    global _url_slot
                    close_all_url_windows()
                    # Open all tabs in parallel — don't block audio playback
                    threads = []
                    for i, u in enumerate(ul[:4]):
                        slot = i % 4
                        t = threading.Thread(target=open_url_in_slot, args=(u["url"], slot), daemon=True)
                        threads.append(t)
                        t.start()
                    _url_slot = (len(ul) % 4)
                threading.Thread(target=_open_on_audio_start, args=(urls_to_open,), daemon=True).start()
            is_playing  = True
            was_playing = True
            arr = np.frombuffer(chunk, dtype=np.float32)
            audio_rms = float(np.sqrt(np.mean(arr**2))) * 4
            out.write(chunk)
            if audio_queue.empty():
                is_playing = False; audio_rms = 0.0
        except queue.Empty:
            if was_playing:
                # Audio just finished — close all URL tabs after short delay
                was_playing = False
                def _close_after_playback():
                    time.sleep(1.5)   # brief pause so user can glance at tabs
                    if not is_playing:  # still idle after delay
                        close_all_url_windows()
                threading.Thread(target=_close_after_playback, daemon=True).start()
            is_playing = False; audio_rms = 0.0
    out.stop_stream(); out.close()

def enqueue_audio(b64):
    if _drop_audio:
        return   # barge-in active — discard stale audio from interrupted response
    raw  = base64.b64decode(b64)
    data = np.frombuffer(raw, dtype=np.float32)
    for i in range(0, len(data), CHUNK_OUT):
        audio_queue.put(data[i:i+CHUNK_OUT].tobytes())

def stop_audio():
    while not audio_queue.empty():
        try: audio_queue.get_nowait()
        except: pass

# ── WEBSOCKET ─────────────────────────────────────────────────────────────────
def on_message(_, message):
    global last_text
    try:
        msg = json.loads(message); ev = msg.get("event","")
        if ev == "media":
            p = msg.get("media",{}).get("payload")
            if p: enqueue_audio(p)
        elif ev == "aiTextStream":
            chunk = msg.get("text_chunk","")
            if chunk:
                if chat_lines and chat_lines[-1][0] == "ai":
                    r, tx = chat_lines[-1]; chat_lines[-1] = (r, tx + chunk)
                    last_text = chat_lines[-1][1][:100]
                else: log_chat("ai", chunk)
        elif ev == "transcription":
            # Handle both string and array payloads
            content = msg.get("content","")
            if isinstance(content, list):
                content = " ".join(str(c) for c in content)
            
            # Normalize role prefix
            role = msg.get("role","ai")
            if isinstance(role, str):
                role = role.lower().strip()
                if role.startswith("user"): role = "user"
                elif role.startswith("assistant") or role == "": role = "ai"
            
            if content:
                log_chat(role, content)
        elif ev in ("clear","clearAudio"):
            stop_audio()
        elif ev == "weestream_urls":
            urls  = msg.get("urls", [])
            qtype = msg.get("type", "")
            log_chat("sys", f"◈ {len(urls)} {qtype} sources — opening when audio starts")
            # Accumulate across multiple tool calls; playback_worker flushes all at once
            for u in urls:
                if len(pending_urls) < 4 and u not in pending_urls:
                    pending_urls.append(u)
        elif ev == "open_tab":
            url   = msg.get("url","")
            label = msg.get("label", url)
            if url and len(pending_urls) < 4:
                log_chat("sys", f"◈ OPEN TAB: {label[:60]} — queued for audio start")
                pending_urls.append({"url": url, "title": label})
        elif ev == "close_all_tabs":
            types = msg.get("tab_types", ["all"])
            log_chat("sys", f"◈ CLOSE TABS: {', '.join(types)}")
            close_all_url_windows()
        elif ev == "close":
            log_chat("sys","◈ SESSION TERMINATED"); set_ws("OFFLINE")
    except Exception as e:
        log_chat("sys", str(e))

def on_open(ws):
    set_ws("CONNECTED"); set_status("SYSTEMS ONLINE — AUTO-STARTING MIC...")
    log_chat("sys","◈ SECURE CHANNEL ESTABLISHED")
    ws.send(json.dumps({"event":"start","token":TOKEN,"campId":CAMP_ID,
                        "codec":"f32_raw","sampleRate":SAMPLE_IN,
                        "asr":"AZURE","startMessage":True}))
    threading.Timer(1.2, start_mic).start()

def on_close(*_):
    set_ws("OFFLINE"); set_status("CONNECTION LOST — PRESS ENTER TO RECONNECT")
    log_chat("sys","◈ CHANNEL CLOSED")

def on_error(*args):
    e = args[1] if len(args) > 1 else args[0]
    set_ws("ERROR"); log_chat("sys", f"◈ ERROR: {e}")

def connect_ws():
    global ws_conn
    set_ws("CONNECTING"); set_status("ESTABLISHING SECURE CHANNEL...")
    log_chat("sys","◈ INITIALIZING WEESTREAM PROTOCOL...")
    ws_conn = websocket.WebSocketApp(WS_URL, on_open=on_open,
                on_message=on_message, on_close=on_close, on_error=on_error)
    threading.Thread(target=ws_conn.run_forever, daemon=True).start()

# ── WAKE WORD ────────────────────────────────────────────────────────────────
WAKE_WORDS = ["jarvis", "hey jarvis", "hey toingg", "toingg"]

def wake_word_listener():
    """
    Single mic thread — dual mode:
      • While AI is SPEAKING (is_playing): short windows, barge-in on 2+ words
      • While IDLE:                         longer windows, wake word detection
    One mic stream at a time — no conflicts.
    """
    try:
        import speech_recognition as sr
    except ImportError:
        log_chat("sys", "◈ SpeechRecognition not found — use Enter to activate mic")
        return

    recognizer = sr.Recognizer()
    recognizer.energy_threshold         = MIC_ENERGY_THRESHOLD
    recognizer.dynamic_energy_threshold = False

    set_status("STANDING BY — SAY 'HEY JARVIS' TO ACTIVATE")
    log_chat("sys", "◈ WAKE WORD ACTIVE — listening for: " + " / ".join(WAKE_WORDS))

    while not stop_event.is_set():
        # Don't compete with active mic stream
        if is_talking:
            time.sleep(0.3)
            continue

        try:
            if is_playing:
                continue
                # # ── BARGE-IN MODE: short window, catch interruption ──
                # recognizer.pause_threshold       = BARGE_IN_PAUSE
                # recognizer.non_speaking_duration = 0.3
                # with sr.Microphone(sample_rate=16000) as source:
                #     try:
                #         audio = recognizer.listen(source, timeout=1.5, phrase_time_limit=3)
                #     except sr.WaitTimeoutError:
                #         continue   # no speech — loop quickly
                # try:
                #     text = recognizer.recognize_google(audio).lower().strip()
                #     words = text.split()
                #     if len(words) >= 2 and is_playing:   # still playing = valid barge-in
                #         global _drop_audio
                #         _drop_audio = True   # drop all stale chunks from this response
                #         log_chat("sys", f"◈ BARGE-IN — \"{text}\" — stopping audio")
                #         stop_audio()
                #         set_status("VOICE INPUT ACTIVE — SPEAK NOW")
                #         time.sleep(0.15)
                #         start_mic()
                # except sr.UnknownValueError:
                #     pass
                # except sr.RequestError as e:
                #     time.sleep(2)

            else:
                # ── WAKE WORD MODE: longer window, full phrase ──
                recognizer.pause_threshold       = MIC_PAUSE_THRESHOLD
                recognizer.non_speaking_duration = 0.8
                with sr.Microphone(sample_rate=16000) as source:
                    recognizer.adjust_for_ambient_noise(source, duration=MIC_CALIBRATION_TIME)
                    try:
                        audio = recognizer.listen(source, timeout=8, phrase_time_limit=6)
                    except sr.WaitTimeoutError:
                        continue
                try:
                    text = recognizer.recognize_google(audio).lower().strip()
                    if any(w in text for w in WAKE_WORDS):
                        log_chat("sys", f"◈ WAKE WORD — \"{text}\"")
                        if ws_status == "CONNECTED":
                            start_mic()
                        else:
                            log_chat("sys", "◈ NOT CONNECTED — press Enter to reconnect")
                except sr.UnknownValueError:
                    pass
                except sr.RequestError as e:
                    log_chat("sys", f"◈ SPEECH API ERROR: {e}")
                    time.sleep(2)

        except Exception as e:
            log_chat("sys", f"◈ LISTENER ERROR: {e}")
            time.sleep(1)

# ── MIC ───────────────────────────────────────────────────────────────────────
def start_mic():
    global is_talking
    if is_talking: return
    def worker():
        global mic_rms, is_talking
        buf    = []
        stream = pa.open(format=pyaudio.paFloat32, channels=1,
                         rate=SAMPLE_IN, input=True, frames_per_buffer=512)
        is_talking = True
        set_status("VOICE INPUT ACTIVE — SPEAK NOW")
        while is_talking:
            try:
                raw = stream.read(512, exception_on_overflow=False)
                arr = np.frombuffer(raw, dtype=np.float32)
                mic_rms = float(np.sqrt(np.mean(arr**2))) * 12
                buf.append(raw)
                if len(buf) >= 4:
                    chunk = b"".join(buf); buf = []
                    if ws_conn and ws_conn.sock:
                        ws_conn.send(json.dumps({"event":"media","media":
                            {"payload": base64.b64encode(chunk).decode()}}))
            except: break
        stream.stop_stream(); stream.close(); mic_rms = 0.0
    threading.Thread(target=worker, daemon=True).start()

def stop_mic():
    global is_talking, _drop_audio
    is_talking  = False
    _drop_audio = False   # allow next AI response to play
    set_status("MIC MUTED — PRESS ENTER TO REACTIVATE")

# ── TERMINAL RESIZE + CENTER ─────────────────────────────────────────────────
def resize_and_center_terminal(cols=74, lines=28):
    """Resize the console window and center it on screen (Windows only)."""
    if _platform.system() != "Windows":
        return
    try:
        os.system(f"mode con: cols={cols} lines={lines}")
        import ctypes, ctypes.wintypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return
        sw, sh = get_screen_size()
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right  - rect.left
        h = rect.bottom - rect.top
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        ctypes.windll.user32.MoveWindow(hwnd, x, y, w, h, True)
    except Exception:
        pass

# ── BOOT ─────────────────────────────────────────────────────────────────────
def boot_sequence():
    for m in [
        "INITIALIZING J.A.R.V.I.S  v5.0...",
        "LOADING NEURAL FRAMEWORK  ▸▸▸  OK",
        "CALIBRATING VOICE MATRIX  ▸▸▸  OK",
        "LAUNCHING VISUAL DISPLAY...",
        "ALL SYSTEMS NOMINAL.",
    ]:
        set_status(m); time.sleep(0.4)

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    os.system("cls" if os.name=="nt" else "clear")
    resize_and_center_terminal()

    # Start HTTP server for visual page
    start_http_server()

    threading.Thread(target=playback_worker, daemon=True).start()

    def ui_loop():
        with Live(make_display(), refresh_per_second=12, screen=True) as live:
            while not stop_event.is_set():
                live.update(make_display())
                time.sleep(1 / 12)

    threading.Thread(target=ui_loop, daemon=True).start()

    boot_sequence()
    connect_ws()

    # Open visual AFTER terminal + WS are live (1.5s delay so terminal shows first)
    threading.Timer(1.5, open_visual_window).start()

    # Start wake word listener
    threading.Thread(target=wake_word_listener, daemon=True).start()

    try:
        while True:
            input()   # Enter = stop mic / reconnect only
            if stop_event.is_set(): break
            if ws_status not in ("CONNECTED",):
                connect_ws(); time.sleep(1); continue
            if is_talking:
                stop_mic()   # Enter stops active mic
            # No else — wake word starts mic, Enter doesn't
    except KeyboardInterrupt:
        pass

    stop_event.set(); stop_audio()
    if ws_conn: ws_conn.close()
    pa.terminate()
    os.system("cls" if os.name=="nt" else "clear")
    print("\n  J.A.R.V.I.S  ◈  OFFLINE\n")

if __name__ == "__main__":
    main()

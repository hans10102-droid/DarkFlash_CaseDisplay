"""CaseDisplay - lightweight driver for the case screen (DarkFlash LCD, USB 1D6B:0148).

  - renders theme\\theme.json (format v2; DarkFlash themes are converted) with LibreHardwareMonitor sensors
  - renders and sends only when the displayed values change (resend every `keepalive_seconds`)
  - screen off while the session is locked or the monitors are asleep (display power off)
  - screen off when Windows shuts down / restarts / signs out (the LCD keeps standby power otherwise)
  - any USB error -> close, reopen, handshake again; theme/render errors keep the last picture
  - do not run together with the DarkFlash app (two writers hang the LCD firmware)
  - reloads config.json / theme when the files change; restarts itself when a .py file changes
  - settings editor: http://127.0.0.1:8765/

Run:  CaseDisplay.exe casedisplay.py              (renamed pythonw.exe + pyvenv.cfg)
      python casedisplay.py --console [--seconds N]
Log:  C:\\ProgramData\\CaseDisplay.log
"""
import argparse, ctypes, io, json, os, sys, threading, time, datetime, traceback
from ctypes import wintypes

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import theme_render
from lcd_protocol import Lcd, LcdError

APP = "CaseDisplay"
LOG_PATH = os.path.join(os.environ.get("ProgramData", BASE), APP + ".log")
CONSOLE = False
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    if CONSOLE:
        print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512_000:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-500:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def single_instance(wait_seconds=20):
    """wait a little so a self-restart can take over from the exiting instance"""
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    end = time.time() + wait_seconds
    while True:
        h = kernel32.CreateMutexW(None, False, "Local\\" + APP)
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return h
        kernel32.CloseHandle(h)
        if time.time() > end:
            return None
        time.sleep(0.5)


def code_stamp():
    return tuple(os.path.getmtime(os.path.join(BASE, f)) for f in sorted(os.listdir(BASE)) if f.endswith(".py"))


def restart_self(lcd):
    log("code changed - restarting")
    lcd.close()
    import subprocess
    subprocess.Popen([sys.executable, os.path.abspath(__file__)] + [x for x in sys.argv[1:] if x != "--seconds"],
                     cwd=BASE, creationflags=0x08000000)  # CREATE_NO_WINDOW
    os._exit(0)


def session_locked():
    """Input desktop cannot be opened while the lock screen (Winlogon desktop) is active."""
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    h = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not h:
        return True
    user32.CloseDesktop(h)
    return False


# ---------------- display power state (monitor sleep) ----------------
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _PBS(ctypes.Structure):  # POWERBROADCAST_SETTING
    _fields_ = [("PowerSetting", _GUID), ("DataLength", wintypes.DWORD), ("Data", ctypes.c_ubyte * 1)]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class DisplayPower:
    """Tracks GUID_CONSOLE_DISPLAY_STATE (0 off, 1 on, 2 dimmed) via a hidden window's WM_POWERBROADCAST,
    and Windows session end (shutdown/restart/sign-out) via WM_QUERYENDSESSION / WM_ENDSESSION.
    The window thread never touches the LCD: it sets `ending`, the main loop turns the screen off and sets `dark`."""
    GUID = (0x6fe69556, 0x704a, 0x47a0, (0x8f, 0x24, 0xc2, 0x8d, 0x93, 0x6f, 0xda, 0x47))
    END_WAIT = 4.0   # seconds WM_ENDSESSION waits for the screen to go dark (Windows force-closes apps after ~5s)

    def __init__(self):
        self.state = 1
        self.ok = False
        self.ending = threading.Event()
        self.dark = threading.Event()
        threading.Thread(target=self._run, name="displaypower", daemon=True).start()

    @property
    def off(self):
        return self.state == 0

    def _run(self):
        try:
            u = user32
            u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u.DefWindowProcW.restype = ctypes.c_ssize_t
            u.CreateWindowExW.restype = wintypes.HWND
            u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            u.RegisterPowerSettingNotification.restype = wintypes.HANDLE
            u.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.POINTER(_GUID), wintypes.DWORD]
            g = _GUID(self.GUID[0], self.GUID[1], self.GUID[2], (ctypes.c_ubyte * 8)(*self.GUID[3]))
            target = bytes(g)

            def wndproc(hwnd, msg, wp, lp):
                if msg == 0x0218 and wp == 0x8013 and lp:  # WM_POWERBROADCAST / PBT_POWERSETTINGCHANGE
                    s = ctypes.cast(lp, ctypes.POINTER(_PBS)).contents
                    if bytes(s.PowerSetting) == target:
                        new = s.Data[0]
                        if new != self.state:
                            log(f"display power: {['off', 'on', 'dimmed'][new] if new < 3 else new}")
                        self.state = new
                elif msg == 0x0011 and not (lp & 0x1):  # WM_QUERYENDSESSION (not ENDSESSION_CLOSEAPP): start turning off now
                    self.dark.clear(); self.ending.set()
                    return 1
                elif msg == 0x0016 and not (lp & 0x1):  # WM_ENDSESSION
                    if wp:
                        if not self.ending.is_set():
                            self.dark.clear(); self.ending.set()
                        if not self.dark.wait(self.END_WAIT):
                            log("session ending - screen off not confirmed in time")
                    elif self.ending.is_set():
                        self.ending.clear(); self.dark.clear()   # shutdown was cancelled
                    return 0
                return u.DefWindowProcW(hwnd, msg, wp, lp)

            self._proc = _WNDPROC(wndproc)   # keep a reference
            hinst = kernel32.GetModuleHandleW(None)
            wc = _WNDCLASS(); wc.lpfnWndProc = self._proc; wc.hInstance = hinst; wc.lpszClassName = APP + "PowerWatch"
            u.RegisterClassW(ctypes.byref(wc))
            hwnd = u.CreateWindowExW(0, wc.lpszClassName, APP, 0, 0, 0, 0, 0, None, None, hinst, None)
            if not hwnd or not u.RegisterPowerSettingNotification(hwnd, ctypes.byref(g), 0):
                log("display power watch unavailable"); return
            self.ok = True
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg)); u.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log(f"display power watch failed: {e}")


def encode_jpeg(img, quality, max_bytes):
    q = quality
    while True:
        b = io.BytesIO()
        img.save(b, "JPEG", quality=q)
        data = b.getvalue()
        if len(data) <= max_bytes or q <= 50:
            return data
        q -= 5


class Files:
    """mtime-based reloader for config and theme"""
    def __init__(self):
        self.cfg_path = os.path.join(BASE, "config.json")
        self.cfg = {}; self.theme = None; self.stamp = None

    def _stamp(self, theme_path):
        paths = [self.cfg_path, theme_path]
        try:
            with open(theme_path, encoding="utf-8-sig") as f:
                bg = json.load(f).get("background", {}).get("path", "")
            if bg:
                paths.append(bg if os.path.isabs(bg) else os.path.join(os.path.dirname(theme_path), bg))
        except Exception:
            pass
        return tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths)

    def refresh(self):
        with open(self.cfg_path, encoding="utf-8-sig") as f:   # tolerate BOM
            cfg = json.load(f)
        tp = cfg.get("theme", "theme\\theme.json")
        tp = tp if os.path.isabs(tp) else os.path.join(BASE, tp)
        st = self._stamp(tp)
        if st != self.stamp:
            self.theme = theme_render.Theme.from_file(tp)
            self.cfg, self.stamp = cfg, st
            return True
        return False


def main():
    global CONSOLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--console", action="store_true")
    ap.add_argument("--seconds", type=int, default=0)
    a = ap.parse_args()
    CONSOLE = a.console

    if not single_instance():
        log("another CaseDisplay instance is running - exit")
        return

    files = Files(); files.refresh()
    lcd = Lcd(log)
    stamp = code_stamp()
    display = DisplayPower()
    webui = None
    try:
        import webui
        webui.start(log)
    except Exception as e:
        log(f"settings editor failed to start: {e}")

    applied = {}               # brightness/rotate currently applied on device
    screen_on = False
    ended = False              # screen turned off for Windows session end
    last_sig, last_jpg, last_send, last_hb = None, None, 0.0, 0.0
    last_render_error = ""
    last_tick = time.time(); t_start = time.time()
    try:
        if webui:
            removed = webui.gc_backgrounds()
            if removed:
                log(f"removed {len(removed)} unused background file(s)")
    except Exception:
        pass
    log(f"started pid={os.getpid()} exe={os.path.basename(sys.executable)} theme={files.cfg.get('theme')} interval={files.cfg.get('interval_seconds')}s")

    while True:
        loop_start = time.time()
        if a.seconds and loop_start - t_start > a.seconds:
            log("test duration reached - exit"); break

        # 1) Windows is shutting down / restarting / signing out: screen off, then leave the device alone
        if display.ending.is_set():
            if not ended:
                ended = True
                try:
                    if lcd.is_open:
                        if screen_on:
                            lcd.realtime(False); time.sleep(0.4)
                        lcd.suspend()
                    log("session ending (shutdown/restart/sign-out) - screen off")
                except Exception as e:
                    log(f"session ending - screen off failed: {e}")
                screen_on = False
            display.dark.set()
            time.sleep(0.2)
            continue
        if ended:
            ended = False; log("session end cancelled")   # step 5 turns the screen back on

        try:
            if not a.seconds and code_stamp() != stamp:
                restart_self(lcd)
            if files.refresh():
                log("config/theme reloaded"); last_sig = None   # keep `applied`: only changed values are re-sent
            cfg = files.cfg
            interval = float(cfg.get("interval_seconds", 2))

            # 2) resumed from sleep (big gap) -> reconnect
            if loop_start - last_tick > max(15, interval * 5) and lcd.is_open:
                log(f"time gap {loop_start - last_tick:.0f}s (sleep/resume) - reconnect")
                lcd.close()
            last_tick = loop_start

            # 3) connect
            if not lcd.is_open:
                lcd.open()
                props = lcd.handshake()
                log(f"connected fw={props.get('version', {}).get('firmware')} brightness={props.get('brightness')} degree={props.get('degree')}")
                applied = {"brightness": props.get("brightness"), "rotate": props.get("degree")}
                lcd.resume(); lcd.heartbeat(); time.sleep(2)
                lcd.realtime(True); screen_on = True; last_sig = None

            # 4) device settings from config
            if "brightness" in cfg and cfg["brightness"] != applied.get("brightness"):
                lcd.brightness(cfg["brightness"]); applied["brightness"] = cfg["brightness"]; log(f"brightness -> {cfg['brightness']}")
            if "rotate" in cfg and cfg["rotate"] != applied.get("rotate"):
                lcd.rotate(cfg["rotate"]); applied["rotate"] = cfg["rotate"]; log(f"rotate -> {cfg['rotate']}")

            # 5) screen off while locked or while the monitors sleep
            reason = None
            if cfg.get("off_when_locked", True) and session_locked():
                reason = "session locked"
            elif cfg.get("off_when_display_off", True) and display.off:
                reason = "monitors asleep"
            if reason and screen_on:
                lcd.realtime(False); time.sleep(0.4); lcd.suspend(); screen_on = False; log(f"{reason} - screen off")
            elif not reason and not screen_on:
                lcd.resume(); time.sleep(1); lcd.realtime(True); screen_on = True; last_sig = None; log("screen on")

            # 6) frame or heartbeat
            now = time.time()
            if screen_on:
                jpg = None
                try:
                    snap = theme_render.read_sensors(cfg.get("lhm_url", "http://localhost:8085/data.json"))
                    theme_render.HISTORY.sample(snap)
                    sig = (files.stamp, files.theme.signature(snap))
                    if sig != last_sig:                       # render only when the picture would change
                        img = files.theme.render(snap)
                        jpg = encode_jpeg(img, int(cfg.get("jpeg_quality", 95)), int(cfg.get("jpeg_max_bytes", 140000)))
                        last_sig = sig
                    last_render_error = ""
                except Exception as e:                        # bad theme etc.: keep the connection and the last picture
                    msg = "".join(traceback.format_exception_only(type(e), e)).strip()
                    if msg != last_render_error:
                        log("render error (keeping last frame): " + msg); last_render_error = msg
                if jpg is not None:
                    lcd.send_jpeg(jpg); last_jpg, last_send = jpg, now
                elif last_jpg is not None and now - last_send >= float(cfg.get("keepalive_seconds", 20)):
                    lcd.send_jpeg(last_jpg); last_send = now
            elif now - last_hb >= 4:
                lcd.heartbeat(); last_hb = now

        except (LcdError, OSError, IOError) as e:
            log(f"device error: {e} - reconnect in 3s")
            lcd.close(); screen_on = False; time.sleep(3)
        except Exception as e:
            log("unexpected: " + "".join(traceback.format_exception_only(type(e), e)).strip())
            lcd.close(); screen_on = False; time.sleep(5)

        elapsed = time.time() - loop_start
        display.ending.wait(max(0.2, float(files.cfg.get("interval_seconds", 2)) - elapsed))   # wake at once on session end

    lcd.close()


if __name__ == "__main__":
    main()

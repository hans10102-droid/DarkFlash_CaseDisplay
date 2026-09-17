"""DarkFlash water-block LCD (USB 1D6B:0148) prototype driver.

Test only: connects, shows a test pattern, then live CPU/GPU stats every N seconds.
Protocol: see ..\\PROTOCOL.md
"""
import argparse, io, json, re, sys, time, urllib.request, datetime
import hid, psutil
from PIL import Image, ImageDraw, ImageFont

VID, PID = 0x1D6B, 0x0148
W, H = 960, 480
LOG = None


def log(msg):
    line = f"{datetime.datetime.now():%H:%M:%S.%f}"[:-3] + " " + msg
    print(line, flush=True)
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------- framing ----------------
def esc(b):
    o = bytearray()
    for x in b:
        if x == 0x5A: o += b"\x5b\x01"
        elif x == 0x5B: o += b"\x5b\x02"
        else: o.append(x)
    return bytes(o)


def unesc(b):
    o = bytearray(); i = 0
    while i < len(b):
        if b[i] == 0x5B and i + 1 < len(b):
            o.append(0x5A if b[i + 1] == 1 else 0x5B); i += 2
        else:
            o.append(b[i]); i += 1
    return bytes(o)


def frame(p):
    L = (len(p) + 5).to_bytes(2, "big")
    cs = (sum(L) + sum(p)) & 0xFF
    return b"\x5a" + esc(L + p + bytes([cs])) + b"\x5a"


class Screen:
    def __init__(self):
        path = None
        for d in hid.enumerate(VID, PID):
            if d["usage_page"] == 0xFF00:
                path = d["path"]
        if not path:
            raise RuntimeError("device 1D6B:0148 (usagePage FF00) not found")
        self.dev = hid.device()
        self.dev.open_path(path)
        self.dev.set_nonblocking(True)
        self.seq = 0
        log("opened " + path.decode(errors="replace"))

    def drain(self, timeout=0.0):
        """read IN reports until a full 5A..5A frame or timeout; return list of decoded payload texts"""
        buf = bytearray(); out = []; end = time.time() + timeout
        while True:
            r = self.dev.read(1025)
            if r:
                buf += bytes(r).rstrip(b"\x00") if False else bytes(r)
                # parse frames
                while True:
                    s = buf.find(0x5A)
                    if s < 0: buf.clear(); break
                    e = buf.find(0x5A, s + 1)
                    if e < 0: break
                    raw = unesc(bytes(buf[s + 1:e]))
                    del buf[:e + 1]
                    if len(raw) < 3: continue
                    L = int.from_bytes(raw[:2], "big"); body = raw[2:-1]; cs = raw[-1]
                    ok = ((raw[0] + raw[1] + sum(body)) & 0xFF) == cs and L == len(body) + 5
                    out.append((ok, body.decode("utf-8", "replace")))
                if out and timeout > 0:
                    return out
                continue
            if time.time() >= end:
                return out
            time.sleep(0.01)

    def req(self, method, cmd, body=None, wait=1.0):
        s = f"{method} {cmd} 1\r\nSeqNumber={self.seq}\r\nDate={int(time.time()*1000)}\r\n"
        if body is not None:
            js = json.dumps(body, separators=(",", ":"))
            s += f"ContentType=json\r\nContentLength={len(js.encode())}\r\n\r\n{js}"
        else:
            s += "\r\n"
        self.seq += 1
        n = self.dev.write(b"\x00" + frame(s.encode()))
        resp = self.drain(wait)
        txt = " | ".join(("" if ok else "[BADCS] ") + t.replace("\r\n", "\\n") for ok, t in resp) or "(no response)"
        log(f"-> {method} {cmd} {body if body is not None else ''} wrote={n} <- {txt[:400]}")
        return resp

    def send_jpeg(self, jpg):
        if other_writer():
            log(f"ABORT: DarkFlash appeared {other_writer()} - stopping before writing")
            sys.exit(4)
        n = (len(jpg) + 999) // 1000
        fid = time.localtime().tm_sec
        t0 = time.perf_counter(); fails = 0
        for i in range(n):
            chunk = jpg[i * 1000:(i + 1) * 1000]
            blk = bytes([fid]) + n.to_bytes(2, "big") + i.to_bytes(2, "big") + b"\x01" + bytes(15) + chunk
            r = self.dev.write(b"\x00\x5c" + len(blk).to_bytes(2, "big") + blk)
            if r < 0: fails += 1
        dt = (time.perf_counter() - t0) * 1000
        extra = self.drain(0.05)
        log(f"frame {len(jpg)}B blocks={n} fid={fid} write_fail={fails} {dt:.0f}ms" + (f" IN: {[t for _, t in extra]}" if extra else ""))


# ---------------- sensors ----------------
def lhm_temps():
    try:
        with urllib.request.urlopen("http://localhost:8085/data.json", timeout=1) as r:
            data = json.load(r)
    except Exception:
        return None, None
    cpu = gpu = None
    def walk(n, trail):
        nonlocal cpu, gpu
        t = n.get("Text", ""); v = n.get("Value", "")
        tr = trail + [t]
        if isinstance(v, str) and "°C" in v:
            val = float(re.sub(r"[^\d.]", "", v.replace(",", ".")) or 0)
            joined = " / ".join(tr)
            if cpu is None and "Ryzen" in joined and re.search(r"Tctl|Tdie|Package", t):
                cpu = val
            if gpu is None and "Radeon RX" in joined and re.search(r"GPU Core", t):
                gpu = val
        for c in n.get("Children", []):
            walk(c, tr)
    walk(data, [])
    return cpu, gpu


def font(size, bold=False):
    for f in (["C:/Windows/Fonts/segoeuib.ttf"] if bold else []) + ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]:
        try: return ImageFont.truetype(f, size)
        except Exception: pass
    return ImageFont.load_default()


def to_jpeg(img):
    b = io.BytesIO(); img.save(b, "JPEG", quality=85); return b.getvalue()


def test_pattern():
    img = Image.new("RGB", (W, H), (10, 10, 30)); d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, H - 1], outline=(255, 255, 0), width=6)
    d.rectangle([0, 0, 120, 60], fill=(220, 30, 30)); d.text((10, 8), "TL", font=font(40, True), fill="white")
    d.rectangle([W - 120, H - 60, W - 1, H - 1], fill=(30, 160, 60)); d.text((W - 110, H - 55), "BR", font=font(40, True), fill="white")
    d.text((180, 150), "TEST 960x480", font=font(96, True), fill=(255, 255, 255))
    d.polygon([(700, 330), (860, 380), (700, 430)], fill=(0, 200, 255))
    d.text((180, 340), "arrow -> right", font=font(48), fill=(0, 200, 255))
    return img


def live_frame(cpu_pct, cpu_t, gpu_t):
    img = Image.new("RGB", (W, H), (8, 8, 16)); d = ImageDraw.Draw(img)
    d.text((40, 20), datetime.datetime.now().strftime("%H:%M:%S"), font=font(64, True), fill=(200, 200, 255))
    d.text((40, 130), f"CPU  {cpu_pct:5.1f} %", font=font(84, True), fill=(255, 255, 255))
    d.text((40, 250), f"CPU  {cpu_t:.0f} °C" if cpu_t is not None else "CPU  -- °C", font=font(84, True), fill=(255, 170, 60))
    d.text((40, 370), f"GPU  {gpu_t:.0f} °C" if gpu_t is not None else "GPU  -- °C", font=font(84, True), fill=(90, 200, 255))
    bar = int(400 * cpu_pct / 100); d.rectangle([520, 160, 520 + 400, 200], outline=(80, 80, 80)); d.rectangle([520, 160, 520 + bar, 200], fill=(255, 255, 255))
    return img


def other_writer():
    """DarkFlash 가 떠 있으면 같은 장치에 동시에 쓰게 되어 LCD 펌웨어가 멈춘다 (09-17 23:01 사고)."""
    return [p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "darkflash.exe"]


def main():
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=150)
    ap.add_argument("--pattern-seconds", type=int, default=20)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--log", default=None)
    ap.add_argument("--theme", default=None, help="DarkFlash theme json -> render with theme_render")
    a = ap.parse_args(); LOG = a.log
    theme = None
    if a.theme:
        import theme_render
        theme = theme_render.Theme.from_file(a.theme)
    me = psutil.Process()
    if other_writer():
        log(f"ABORT: DarkFlash is running {other_writer()} - refusing to open the device")
        sys.exit(3)
    s = Screen()
    s.req("POST", "conn")
    s.req("POST", "power", {"event": "resume"})
    s.req("STATE", "all", {"heartbeat": 1})
    time.sleep(2)
    s.req("POST", "realtimeDisplay", {"enable": True})
    t_start = time.time()
    if a.pattern_seconds > 0:
        s.send_jpeg(to_jpeg(test_pattern()))
        time.sleep(a.pattern_seconds)
    psutil.cpu_percent(None)
    cpu0 = me.cpu_times(); t0 = time.time(); frames = 0
    while time.time() - t_start < a.seconds:
        time.sleep(a.interval)
        if theme:
            img = theme.render(theme_render.read_sensors())
        else:
            cp = psutil.cpu_percent(None); ct, gt = lhm_temps()
            img = live_frame(cp, ct, gt)
        s.send_jpeg(to_jpeg(img)); frames += 1
    cpu1 = me.cpu_times(); el = time.time() - t0
    used = (cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)
    log(f"DONE frames={frames} elapsed={el:.0f}s own_cpu={used:.2f}s -> {100*used/el/psutil.cpu_count():.3f}% of total CPU, rss={me.memory_info().rss/1e6:.0f}MB")


if __name__ == "__main__":
    main()

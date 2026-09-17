"""DarkFlash water-block LCD (USB 1D6B:0148) HID protocol.  See ..\\DarkFlashProtocol\\PROTOCOL.md"""
import json, time
import hid

VID, PID, USAGE_PAGE = 0x1D6B, 0x0148, 0xFF00


class LcdError(Exception):
    pass


def _esc(b):
    o = bytearray()
    for x in b:
        if x == 0x5A: o += b"\x5b\x01"
        elif x == 0x5B: o += b"\x5b\x02"
        else: o.append(x)
    return bytes(o)


def _unesc(b):
    o = bytearray(); i = 0
    while i < len(b):
        if b[i] == 0x5B and i + 1 < len(b):
            o.append(0x5A if b[i + 1] == 1 else 0x5B); i += 2
        else:
            o.append(b[i]); i += 1
    return bytes(o)


def frame(payload):
    L = (len(payload) + 5).to_bytes(2, "big")
    cs = (sum(L) + sum(payload)) & 0xFF
    return b"\x5a" + _esc(L + payload + bytes([cs])) + b"\x5a"


def find_path():
    for d in hid.enumerate(VID, PID):
        if d["usage_page"] == USAGE_PAGE:
            return d["path"]
    return None


class Lcd:
    def __init__(self, log=print):
        self.log = log
        self.dev = None
        self.seq = 0

    # ---------- transport ----------
    def open(self):
        path = find_path()
        if not path:
            raise LcdError("device not present")
        d = hid.device()
        d.open_path(path)
        d.set_nonblocking(True)
        self.dev = d
        self.seq = 0

    def close(self):
        if self.dev:
            try: self.dev.close()
            except Exception: pass
        self.dev = None

    @property
    def is_open(self):
        return self.dev is not None

    def _write(self, data):
        n = self.dev.write(data)
        if n is None or n < 0:
            raise LcdError(f"write failed ({n})")
        return n

    def _read_frames(self, timeout):
        buf = bytearray(); out = []; end = time.time() + timeout
        while True:
            r = self.dev.read(1025)
            if r:
                buf += bytes(r)
                while True:
                    s = buf.find(0x5A)
                    if s < 0: buf.clear(); break
                    e = buf.find(0x5A, s + 1)
                    if e < 0: break
                    raw = _unesc(bytes(buf[s + 1:e])); del buf[:e + 1]
                    if len(raw) < 3: continue
                    body, cs = raw[2:-1], raw[-1]
                    ok = ((raw[0] + raw[1] + sum(body)) & 0xFF) == cs
                    out.append((ok, body.decode("utf-8", "replace")))
                if out:
                    return out
                continue
            if time.time() >= end:
                return out
            time.sleep(0.005)

    # ---------- messages ----------
    def request(self, method, cmd, body=None, wait=1.0, require_ok=True):
        s = f"{method} {cmd} 1\r\nSeqNumber={self.seq}\r\nDate={int(time.time() * 1000)}\r\n"
        if body is not None:
            js = json.dumps(body, separators=(",", ":"))
            s += f"ContentType=json\r\nContentLength={len(js.encode())}\r\n\r\n{js}"
        else:
            s += "\r\n"
        self.seq += 1
        self._write(b"\x00" + frame(s.encode()))
        resp = self._read_frames(wait)
        text = resp[0][1] if resp else ""
        if require_ok and " 200" not in text.split("\r\n", 1)[0]:
            raise LcdError(f"{method} {cmd}: bad/no response {text[:80]!r}")
        return text

    def handshake(self):
        """conn -> returns device properties dict"""
        text = self.request("POST", "conn")
        props = {}
        if "\r\n\r\n" in text:
            try: props = json.loads(text.split("\r\n\r\n", 1)[1])
            except Exception: pass
        return props

    def resume(self):
        self.request("POST", "power", {"event": "resume"})

    def suspend(self):
        self.request("POST", "power", {"event": "suspend"})

    def heartbeat(self):
        self.request("STATE", "all", {"heartbeat": 1})

    def realtime(self, enable):
        self.request("POST", "realtimeDisplay", {"enable": bool(enable)})

    def brightness(self, value):
        self.request("POST", "brightness", {"value": max(0, min(100, int(value)))})

    def rotate(self, degree):
        if degree not in (0, 90, 180, 270):
            raise ValueError("degree must be 0/90/180/270")
        self.request("POST", "rotate", {"degree": degree})

    def send_jpeg(self, jpg):
        n = (len(jpg) + 999) // 1000
        fid = time.localtime().tm_sec
        for i in range(n):
            chunk = jpg[i * 1000:(i + 1) * 1000]
            blk = bytes([fid]) + n.to_bytes(2, "big") + i.to_bytes(2, "big") + b"\x01" + bytes(15) + chunk
            self._write(b"\x00\x5c" + len(blk).to_bytes(2, "big") + blk)
        # drain any stray IN reports so the buffer never fills
        self._read_frames(0)
        return n

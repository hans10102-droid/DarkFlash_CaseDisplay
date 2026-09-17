"""Local settings editor for CaseDisplay.  http://127.0.0.1:8765/  (localhost only)"""
import base64, io, json, os, re, threading, time, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import fonts
import theme_render

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "config.json")
PORT = 8765


def _load_cfg():
    with open(CFG, encoding="utf-8-sig") as f:
        return json.load(f)


def _theme_path(cfg):
    tp = cfg.get("theme", "theme\\theme.json")
    return tp if os.path.isabs(tp) else os.path.join(BASE, tp)


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------- saved setting records ----------------
HISTORY_KEEP = 50
DEVICE_KEYS = ("brightness", "rotate", "interval_seconds", "off_when_locked", "off_when_display_off")


def _records_dir(cfg, kind):
    d = os.path.join(os.path.dirname(_theme_path(cfg)), "records", kind)
    os.makedirs(d, exist_ok=True)
    return d


def _record(folder, rid, name, cfg, theme):
    if theme is None:
        with open(_theme_path(_load_cfg()), encoding="utf-8-sig") as f:
            theme = json.load(f)
    _write_json(os.path.join(folder, rid + ".json"), {
        "name": name, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {k: cfg[k] for k in DEVICE_KEYS if k in cfg},
        "theme": theme,
    })


def _list_records(folder):
    out = []
    for fn in sorted(os.listdir(folder), reverse=True):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, fn), encoding="utf-8-sig") as f:
                r = json.load(f)
            out.append({"id": fn[:-5], "name": r.get("name") or "", "saved_at": r.get("saved_at", ""),
                        "widgets": len(r.get("theme", {}).get("osd", {}).get("widget", [])),
                        "brightness": r.get("config", {}).get("brightness")})
        except Exception:
            pass
    return out


def _prune(folder, keep):
    files = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
    for f in files[:-keep]:
        try: os.remove(os.path.join(folder, f))
        except OSError: pass


class Handler(BaseHTTPRequestHandler):
    server_version = "CaseDisplay"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---------------- GET ----------------
    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(BASE, "editor.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                cfg = _load_cfg()
                tp = _theme_path(cfg)
                with open(tp, encoding="utf-8-sig") as f:
                    theme = json.load(f)
                theme_render.read_sensors(cfg.get("lhm_url", "http://localhost:8085/data.json"))
                return self._send(200, {
                    "config": cfg, "theme": theme,
                    "sources": theme_render.source_catalog(),
                    "fonts": fonts.names(),
                    "canvas": {"w": theme_render.W, "h": theme_render.H},
                })
            if u.path == "/api/records":
                cfg = _load_cfg()
                return self._send(200, {"presets": _list_records(_records_dir(cfg, "presets")),
                                        "history": _list_records(_records_dir(cfg, "history"))})
            if u.path == "/api/log":
                p = os.path.join(os.environ.get("ProgramData", BASE), "CaseDisplay.log")
                with open(p, encoding="utf-8", errors="replace") as f:
                    return self._send(200, "".join(f.readlines()[-60:]), "text/plain; charset=utf-8")
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    # ---------------- POST ----------------
    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/render":
                req = json.loads(self._body() or b"{}")
                cfg = _load_cfg()
                theme = theme_render.Theme(req["theme"], os.path.dirname(_theme_path(cfg)))
                sensors = theme_render.read_sensors(cfg.get("lhm_url", "http://localhost:8085/data.json"))
                boxes = []
                img = theme.render(sensors, boxes=boxes)
                b = io.BytesIO(); img.save(b, "JPEG", quality=88)
                return self._send(200, {"image": "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode(),
                                        "boxes": boxes, "scale": theme.scale, "canvas_w": theme.canvas_w})
            if u.path == "/api/save":
                req = json.loads(self._body() or b"{}")
                cfg = _load_cfg()
                if "config" in req:
                    new = dict(cfg)
                    for k in ("brightness", "rotate", "interval_seconds", "keepalive_seconds", "off_when_locked", "off_when_display_off", "jpeg_quality"):
                        if k in req["config"]:
                            new[k] = req["config"][k]
                    new["brightness"] = max(0, min(100, int(new.get("brightness", 21))))
                    if int(new.get("rotate", 270)) not in (0, 90, 180, 270):
                        return self._send(400, {"error": "rotate must be 0/90/180/270"})
                    new["interval_seconds"] = max(0.5, float(new.get("interval_seconds", 2)))
                    _write_json(CFG, new)
                    cfg = new
                if "theme" in req:
                    _write_json(_theme_path(cfg), req["theme"])
                # every save is recorded automatically (config + theme)
                _record(_records_dir(cfg, "history"), time.strftime("%Y%m%d_%H%M%S"), "", cfg, req.get("theme"))
                _prune(_records_dir(cfg, "history"), HISTORY_KEEP)
                return self._send(200, {"ok": True})
            if u.path == "/api/records":
                cfg = _load_cfg()
                return self._send(200, {"presets": _list_records(_records_dir(cfg, "presets")),
                                        "history": _list_records(_records_dir(cfg, "history"))})
            if u.path == "/api/records/save":
                req = json.loads(self._body() or b"{}")
                name = re.sub(r'[\\/:*?"<>|]+', "_", (req.get("name") or "").strip())[:60]
                if not name:
                    return self._send(400, {"error": "이름을 입력하세요"})
                cfg = _load_cfg()
                _record(_records_dir(cfg, "presets"), name, name, req.get("config") or cfg, req.get("theme"))
                return self._send(200, {"ok": True, "id": name})
            if u.path in ("/api/records/load", "/api/records/delete"):
                req = json.loads(self._body() or b"{}")
                kind = "presets" if req.get("kind") == "presets" else "history"
                rid = os.path.basename(req.get("id") or "")
                path = os.path.join(_records_dir(_load_cfg(), kind), rid + ".json")
                if not os.path.exists(path):
                    return self._send(404, {"error": "기록을 찾을 수 없습니다"})
                if u.path.endswith("delete"):
                    os.remove(path)
                    return self._send(200, {"ok": True})
                with open(path, encoding="utf-8-sig") as f:
                    return self._send(200, json.load(f))
            if u.path == "/api/background":
                q = parse_qs(u.query)
                name = os.path.basename((q.get("name") or ["background.png"])[0])
                ext = os.path.splitext(name)[1].lower()
                if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"):
                    return self._send(400, {"error": "image file only"})
                data = self._body()
                from PIL import Image
                img = Image.open(io.BytesIO(data)).convert("RGB")      # validate + normalize
                cfg = _load_cfg()
                tdir = os.path.dirname(_theme_path(cfg))
                fn = time.strftime("background_%Y%m%d_%H%M%S.png")
                img.resize((theme_render.W, theme_render.H), Image.LANCZOS).save(os.path.join(tdir, fn))
                return self._send(200, {"path": fn})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def start(log=print):
    def run():
        for attempt in range(10):
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
                log(f"settings editor on http://127.0.0.1:{PORT}/")
                srv.serve_forever()
                return
            except OSError as e:
                log(f"web ui bind failed ({e}) - retry"); time.sleep(5)
    t = threading.Thread(target=run, name="webui", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    start(); print(f"http://127.0.0.1:{PORT}/")
    while True: time.sleep(3600)

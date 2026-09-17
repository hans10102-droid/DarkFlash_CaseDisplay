"""Settings editor for CaseDisplay.  http://127.0.0.1:8765/  (localhost only)"""
import base64, glob, io, json, os, re, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageOps

import fonts
import theme_render

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "config.json")
PORT = 8765
HISTORY_KEEP = 50
DEVICE_KEYS = ("brightness", "rotate", "interval_seconds", "off_when_locked", "off_when_display_off")
Image.MAX_IMAGE_PIXELS = 200_000_000


# ---------------- files ----------------
def _load_cfg():
    with open(CFG, encoding="utf-8-sig") as f:
        return json.load(f)


def _theme_path(cfg=None):
    tp = (cfg or _load_cfg()).get("theme", "theme\\theme.json")
    return tp if os.path.isabs(tp) else os.path.join(BASE, tp)


def _theme_dir(cfg=None):
    return os.path.dirname(_theme_path(cfg))


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _v2(theme):
    return theme if theme.get("version") == theme_render.FORMAT_VERSION else theme_render.convert_darkflash(theme)


# ---------------- setting records ----------------
def _records_dir(kind, cfg=None):
    d = os.path.join(_theme_dir(cfg), "records", kind)
    os.makedirs(d, exist_ok=True)
    return d


def _record(folder, rid, name, cfg, theme):
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
            th = _v2(r.get("theme") or {})
            out.append({"id": fn[:-5], "name": r.get("name") or "", "saved_at": r.get("saved_at", ""),
                        "widgets": len(th.get("widgets", [])), "brightness": (r.get("config") or {}).get("brightness")})
        except Exception:
            pass
    return out


def _prune(folder, keep):
    for f in sorted(x for x in os.listdir(folder) if x.endswith(".json"))[:-keep]:
        try:
            os.remove(os.path.join(folder, f))
        except OSError:
            pass


def gc_backgrounds(min_age_hours=24):
    """delete editor-generated background_* images that neither the theme nor any record uses (older than a day)"""
    tdir = _theme_dir()
    used = set()
    jsons = [_theme_path()] + glob.glob(os.path.join(tdir, "records", "*", "*.json"))
    for p in jsons:
        try:
            with open(p, encoding="utf-8-sig") as f:
                d = json.load(f)
            th = d.get("theme") if "theme" in d else d          # record file vs theme file
            used.add(os.path.basename(((th or {}).get("background") or {}).get("path", "")))
        except Exception:
            pass
    removed = []
    for f in glob.glob(os.path.join(tdir, "background_*.*")):   # only files the editor generated
        name = os.path.basename(f)
        if name in used or time.time() - os.path.getmtime(f) < min_age_hours * 3600:
            continue
        try:
            os.remove(f); removed.append(name)
        except OSError:
            pass
    return removed


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CaseDisplay"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self):
        return json.loads(self._body() or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(BASE, "editor.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                cfg = _load_cfg()
                with open(_theme_path(cfg), encoding="utf-8-sig") as f:
                    theme = _v2(json.load(f))
                snap = theme_render.read_sensors(cfg.get("lhm_url", "http://localhost:8085/data.json"))
                return self._send(200, {"config": cfg, "theme": theme, "sources": theme_render.source_catalog(snap),
                                        "fonts": fonts.names(), "canvas": {"w": theme_render.W, "h": theme_render.H}})
            if u.path == "/api/records":
                return self._send(200, {"presets": _list_records(_records_dir("presets")), "history": _list_records(_records_dir("history"))})
            if u.path == "/api/log":
                p = os.path.join(os.environ.get("ProgramData", BASE), "CaseDisplay.log")
                with open(p, encoding="utf-8", errors="replace") as f:
                    return self._send(200, "".join(f.readlines()[-60:]), "text/plain; charset=utf-8")
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/render":
                req = self._json()
                cfg = _load_cfg()
                theme = theme_render.Theme(req["theme"], _theme_dir(cfg))
                snap = theme_render.read_sensors(cfg.get("lhm_url", "http://localhost:8085/data.json"))
                scale = max(1.0, min(2.0, float(req.get("scale", 1))))
                boxes = []
                img = theme.render(snap, boxes=boxes, scale=scale)
                b = io.BytesIO(); img.save(b, "JPEG", quality=92)
                return self._send(200, {"image": "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode(), "boxes": boxes})

            if u.path == "/api/save":
                req = self._json()
                cfg = _load_cfg()
                if "config" in req:
                    new = dict(cfg)
                    for k in DEVICE_KEYS + ("keepalive_seconds",):
                        if k in req["config"]:
                            new[k] = req["config"][k]
                    new["brightness"] = max(0, min(100, int(new.get("brightness", 21))))
                    if int(new.get("rotate", 270)) not in (0, 90, 180, 270):
                        return self._send(400, {"error": "rotate must be 0/90/180/270"})
                    new["interval_seconds"] = max(0.5, float(new.get("interval_seconds", 2)))
                    _write_json(CFG, new)
                    cfg = new
                theme = req.get("theme")
                if theme is not None:
                    theme = _v2(theme)
                    theme_render.Theme(json.loads(json.dumps(theme)), _theme_dir(cfg)).render(theme_render.Snapshot())  # validate before writing
                    _write_json(_theme_path(cfg), theme)
                else:
                    with open(_theme_path(cfg), encoding="utf-8-sig") as f:
                        theme = _v2(json.load(f))
                _record(_records_dir("history", cfg), time.strftime("%Y%m%d_%H%M%S"), "", cfg, theme)
                _prune(_records_dir("history", cfg), HISTORY_KEEP)
                gc_backgrounds()
                return self._send(200, {"ok": True})

            if u.path == "/api/background":
                name = os.path.basename((q.get("name") or ["background.png"])[0])
                if os.path.splitext(name)[1].lower() not in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"):
                    return self._send(400, {"error": "image file only"})
                img = ImageOps.exif_transpose(Image.open(io.BytesIO(self._body()))).convert("RGB")
                if "w" in q and "h" in q:     # crop in original pixels, then high-quality downscale
                    x, y = float(q["x"][0]), float(q["y"][0])
                    cw, ch = float(q["w"][0]), float(q["h"][0])
                    x, y = max(0.0, x), max(0.0, y)
                    cw, ch = min(cw, img.width - x), min(ch, img.height - y)
                    img = img.crop((round(x), round(y), round(x + cw), round(y + ch)))
                else:                          # no crop given: cover-fit to 2:1
                    img = ImageOps.fit(img, (theme_render.W, theme_render.H), Image.LANCZOS)
                img = img.resize((theme_render.W, theme_render.H), Image.LANCZOS)
                fn = time.strftime("background_%Y%m%d_%H%M%S.png")
                img.save(os.path.join(_theme_dir(), fn))
                return self._send(200, {"path": fn})

            if u.path == "/api/records/save":
                req = self._json()
                name = re.sub(r'[\\/:*?"<>|]+', "_", (req.get("name") or "").strip())[:60]
                if not name:
                    return self._send(400, {"error": "이름을 입력하세요"})
                cfg = _load_cfg()
                theme = req.get("theme")
                if theme is None:
                    with open(_theme_path(cfg), encoding="utf-8-sig") as f:
                        theme = json.load(f)
                _record(_records_dir("presets", cfg), name, name, req.get("config") or cfg, _v2(theme))
                return self._send(200, {"ok": True, "id": name})

            if u.path in ("/api/records/load", "/api/records/delete"):
                req = self._json()
                kind = "presets" if req.get("kind") == "presets" else "history"
                path = os.path.join(_records_dir(kind), os.path.basename(req.get("id") or "") + ".json")
                if not os.path.exists(path):
                    return self._send(404, {"error": "기록을 찾을 수 없습니다"})
                if u.path.endswith("delete"):
                    os.remove(path)
                    return self._send(200, {"ok": True})
                with open(path, encoding="utf-8-sig") as f:
                    r = json.load(f)
                r["theme"] = _v2(r.get("theme") or {})
                return self._send(200, r)

            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def start(log=print):
    def run():
        for _ in range(10):
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
    while True:
        time.sleep(3600)

"""DarkFlash-compatible theme renderer (960x480).

Theme = DarkFlash customizationTheme JSON:
  background.path  (relative to theme file or absolute)
  osd.widget[]     type text | data | bar, property[0] in editor canvas units (canvasProperties.width ~571.43)
Data sources: DarkFlash names (CPU Temperature, ...), "Time" / "Date" / "Weekday", or "lhm:<SensorId>".
"""
import datetime, json, os, re, time, urllib.request
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont
import fonts

W, H = 960, 480
DEFAULT_CANVAS_W = 571.4285714285714

# DarkFlash source name -> (LHM SensorId, unit, converter)
SOURCES = {
    "CPU Temperature": ("/amdcpu/0/temperature/2", "°C", float),
    "CPU Usage":       ("/amdcpu/0/load/0", "%", float),
    "CPU Power":       ("/amdcpu/0/power/0", "W", float),
    "CPU Fan Speed":   ("/lpc/it8696e/0/fan/0", "RPM", float),
    "GPU Temperature": ("/gpu-amd/5/temperature/0", "°C", float),
    "GPU Hot Spot":    ("/gpu-amd/5/temperature/7", "°C", float),
    "GPU Usage":       ("/gpu-amd/5/load/0", "%", float),
    "GPU Power":       ("/gpu-amd/5/power/3", "W", float),
    "GPU Fan Speed":   ("/gpu-amd/5/fan/0", "RPM", float),
    "GPU Memory Used": ("/gpu-amd/5/smalldata/0", "MB", float),
    "Memory Usage":    ("/ram/load/0", "%", float),
    "Memory Used":     ("/ram/data/0", "MB", lambda v: v * 1024.0),
}
TIME_SOURCES = {"Time": "%H:%M", "Time (sec)": "%H:%M:%S", "Date": "%Y-%m-%d", "Weekday": "%a"}

LAST_UNITS = {}      # SensorId -> unit text from LHM
LAST_LABELS = {}     # SensorId -> "Hardware / Group / Name"


def read_sensors(url="http://localhost:8085/data.json"):
    vals = {}
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            data = json.load(r)
    except Exception:
        return vals
    def walk(n, trail):
        t = n.get("Text", "")
        tr = trail + [t]
        sid = n.get("SensorId")
        if sid:
            raw = str(n.get("Value", ""))
            m = re.match(r"\s*(-?[\d.,]+)\s*(.*)$", raw.replace(",", "."))
            if m:
                try:
                    vals[sid] = float(m.group(1))
                    LAST_UNITS[sid] = m.group(2).strip()
                    LAST_LABELS[sid] = " / ".join(x for x in tr[-3:] if x)
                except ValueError:
                    pass
        for c in n.get("Children", []):
            walk(c, tr)
    walk(data, [])
    return vals


def source_text(src, sensors, now=None):
    """-> (display text, numeric value or None)"""
    if src in TIME_SOURCES:
        return (now or datetime.datetime.now()).strftime(TIME_SOURCES[src]), None
    if src and src.startswith("lhm:"):
        sid = src[4:]
        v = sensors.get(sid)
        unit = LAST_UNITS.get(sid, "").replace(" ", "")
        return (f"{v:.0f}{unit}" if v is not None else f"--{unit}"), v
    if src in SOURCES:
        sid, unit, conv = SOURCES[src]
        v = sensors.get(sid)
        v = conv(v) if v is not None else None
        return (f"{v:.0f}{unit}" if v is not None else f"--{unit}"), v
    return "?", None


def source_catalog():
    """for the editor: [{id, label}]"""
    out = [{"id": k, "label": k} for k in SOURCES] + [{"id": k, "label": k} for k in TIME_SOURCES]
    for sid in sorted(LAST_LABELS):
        out.append({"id": "lhm:" + sid, "label": "LHM: " + LAST_LABELS[sid]})
    return out


def parse_color(s, default=(255, 255, 255, 255)):
    if not s: return default
    s = s.strip()
    m = re.match(r"rgba?\(([^)]+)\)", s)
    if m:
        p = [x.strip() for x in m.group(1).split(",")]
        r, g, b = (int(float(p[i])) for i in range(3))
        a = int(round(float(p[3]) * 255)) if len(p) > 3 else 255
        return (r, g, b, a)
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3: h = "".join(c * 2 for c in h)
        a = int(h[6:8], 16) if len(h) >= 8 else 255
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)
    return default


@lru_cache(maxsize=128)
def get_font(family, bold, size):
    size = max(6, int(round(size)))
    path = fonts.resolve(family, bold) or fonts.resolve("Segoe UI", bold)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


@lru_cache(maxsize=4)
def load_background(path):
    try:
        return Image.open(path).convert("RGB").resize((W, H), Image.LANCZOS)
    except Exception:
        return Image.new("RGB", (W, H), (0, 0, 0))


class Theme:
    def __init__(self, theme, base_dir=""):
        self.t = theme
        self.base_dir = base_dir
        widgets = theme.setdefault("osd", {}).setdefault("widget", [])
        cw = None
        for w in widgets:
            cp = (w.get("property") or [{}])[0].get("canvasProperties")
            if cp:
                cw = float(cp["width"]); break
        self.canvas_w = cw or DEFAULT_CANVAS_W
        self.scale = W / self.canvas_w
        self.widgets = widgets

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8-sig") as f:
            return cls(json.load(f), os.path.dirname(os.path.abspath(path)))

    def background_path(self):
        p = self.t.get("background", {}).get("path", "")
        return p if not p or os.path.isabs(p) else os.path.join(self.base_dir, p)

    def render(self, sensors, now=None, boxes=None):
        """boxes: optional list to receive {id, x, y, w, h} in 960x480 pixels"""
        s = self.scale
        img = load_background(self.background_path()).copy().convert("RGBA")
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        order = sorted(self.widgets, key=lambda w: (w.get("layerIndex", 0), 0 if w.get("type") == "bar" else 1))
        for w in order:
            if w.get("hidden"):
                continue
            p = (w.get("property") or [{}])[0]
            x, y = float(p.get("x", 0)) * s, float(p.get("y", 0)) * s
            typ = w.get("type")
            if typ == "bar":
                bw, bh = float(p.get("width", 80)) * s, float(p.get("height", 20)) * s
                _, v = source_text(p.get("source"), sensors, now)
                lo, hi = float(p.get("minValue", 0)), float(p.get("maxValue", 100))
                frac = 0.0 if v is None or hi <= lo else max(0.0, min(1.0, (v - lo) / (hi - lo)))
                d.rectangle([x, y, x + bw, y + bh], fill=parse_color(p.get("bgColor"), (204, 204, 204, 255)))
                c0, c1 = parse_color(p.get("startColor")), parse_color(p.get("endColor"))
                for i in range(int(bw * frac)):
                    t = i / max(1.0, bw - 1)
                    d.line([x + i, y, x + i, y + bh], fill=tuple(int(c0[k] + (c1[k] - c0[k]) * t) for k in range(4)))
                if boxes is not None:
                    boxes.append({"id": w.get("id"), "x": x, "y": y, "w": bw, "h": bh})
            elif typ in ("data", "text"):
                fs = float(p.get("fontSize", 16)) * s
                font = get_font(p.get("fontFamily") or "Segoe UI", bool(p.get("bold")), fs)
                txt = source_text(p.get("source"), sensors, now)[0] if typ == "data" else str(p.get("content", ""))
                col = parse_color(p.get("textColor"))
                col = col[:3] + (int(col[3] * float(p.get("opacity", 1))),)
                top = font.getbbox("0")[1]
                tx, ty = x + 2 * s, y - top + fs * 0.12
                d.text((tx, ty), txt, font=font, fill=col)
                if boxes is not None:
                    bb = d.textbbox((tx, ty), txt or " ", font=font)
                    boxes.append({"id": w.get("id"), "x": min(x, bb[0]), "y": min(y, bb[1]), "w": max(bb[2] - x, 8), "h": max(bb[3] - y, fs)})
        img.alpha_composite(layer)
        return img.convert("RGB")

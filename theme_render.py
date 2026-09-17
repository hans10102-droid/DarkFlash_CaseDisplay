"""CaseDisplay theme renderer (960x480).

Theme format v2 (coordinates in screen pixels):
{
  "version": 2,
  "background": {"path": "background_x.png"},          # relative to the theme file
  "widgets": [ {"id", "type", "x", "y", ...}, ... ]     # drawn in list order (later = on top)
}
Widget types
  text   content, font{family,size,bold}, color, card
  value  source, decimals, unit(bool), font, color, colorRanges[{from, color}], card
  bar    source, w, h, min, max, bg, colorStart, colorEnd, segments, gap, radius, card
  ring   source, size, thickness, min, max, bg, colorStart, colorEnd, startAngle, sweep, roundCap,
         showValue, font, color, label, labelFont, labelColor, colorRanges, card
  gauge  source, size, thickness, min, max, bg, colorStart, colorEnd, ticks, tickColor, needleColor,
         showValue, font, color, label, labelFont, labelColor, colorRanges, card
  graph  source, w, h, seconds, min, max(null = auto), style(area|line), lineColor, fillColor,
         lineWidth, grid, gridColor, card
card = {"enabled", "color", "radius", "padding"}
Sources: friendly names (see SOURCES), "Time" / "Time (sec)" / "Date" / "Weekday", or "lhm:<SensorId>".
DarkFlash (v1) themes are converted by convert_darkflash().
"""
import collections, datetime, json, math, os, re, threading, time, urllib.request
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont
import fonts

W, H = 960, 480
FORMAT_VERSION = 2
SS = 3  # supersampling factor for arcs / lines


# ============================ sensors ============================
# friendly source -> (hardware regex, group, name regex, target unit)
_DGPU = r"Radeon RX|Radeon Pro|GeForce|Quadro|RTX|Arc A|Arc B"
SOURCES = {
    "CPU Temperature": (r"Ryzen|Intel|CPU", "Temperatures", r"Tctl|Package|CPU", "°C"),
    "CPU Usage":       (r"Ryzen|Intel|CPU", "Load", r"CPU Total", "%"),
    "CPU Power":       (r"Ryzen|Intel|CPU", "Powers", r"Package", "W"),
    "CPU Fan Speed":   (r"ITE|Nuvoton|NCT|Fintek|Winbond", "Fans", r"CPU|Fan #1\b", "RPM"),
    "GPU Temperature": (_DGPU, "Temperatures", r"^GPU Core$", "°C"),
    "GPU Hot Spot":    (_DGPU, "Temperatures", r"Hot Spot", "°C"),
    "GPU Usage":       (_DGPU, "Load", r"^GPU Core$", "%"),
    "GPU Power":       (_DGPU, "Powers", r"GPU Package|GPU Power|^GPU Core$", "W"),
    "GPU Fan Speed":   (_DGPU, "Fans", r"GPU Fan", "RPM"),
    "GPU Memory Used": (_DGPU, "Data", r"^GPU Memory Used$", "MB"),
    "Memory Usage":    (r"^Total Memory$|^Generic Memory$|^Memory$", "Load", r"^Memory$", "%"),
    "Memory Used":     (r"^Total Memory$|^Generic Memory$|^Memory$", "Data", r"^Memory Used$", "MB"),
}
TIME_SOURCES = {"Time": "%H:%M", "Time (sec)": "%H:%M:%S", "Date": "%Y-%m-%d", "Weekday": "%a"}
_UNIT_SCALE = {("GB", "MB"): 1024.0, ("MB", "GB"): 1 / 1024.0, ("KB", "MB"): 1 / 1024.0}


class Snapshot:
    """One read of the LHM tree: sid -> (value, unit); sensors list for name-based lookup."""
    def __init__(self, sensors=None):
        self.values = {}      # sid -> (value, unit)
        self.labels = {}      # sid -> "hardware / group / name"
        self.entries = []     # (hardware, group, name, sid)
        self.time = time.time()

    def resolve(self, source):
        if source.startswith("lhm:"):
            return source[4:]
        return _resolver(self, source)

    def value(self, source):
        """-> (numeric value or None, unit text)"""
        if source in SOURCES:
            sid = self.resolve(source)
            v = self.values.get(sid)
            target = SOURCES[source][3]
            if v is None:
                return None, target
            val, unit = v
            return val * _UNIT_SCALE.get((unit, target), 1.0), target
        if source and source.startswith("lhm:"):
            v = self.values.get(source[4:])
            return (v[0], v[1].replace(" ", "")) if v else (None, "")
        return None, ""


_resolve_cache = {"key": None, "map": {}}


def _resolver(snap, source):
    key = len(snap.entries)
    if _resolve_cache["key"] != key:
        _resolve_cache["key"], _resolve_cache["map"] = key, {}
    m = _resolve_cache["map"]
    if source not in m:
        hw_re, group, name_re, _ = SOURCES[source]
        sid = None
        for hw, grp, name, s in snap.entries:
            if grp == group and re.search(hw_re, hw) and re.search(name_re, name):
                sid = s; break
        m[source] = sid
    return m[source]


def read_sensors(url="http://localhost:8085/data.json"):
    snap = Snapshot()
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            data = json.load(r)
    except Exception:
        return snap
    def walk(n, trail):
        tr = trail + [n.get("Text", "")]
        sid = n.get("SensorId")
        if sid:
            m = re.match(r"\s*(-?[\d.,]+)\s*(.*)$", str(n.get("Value", "")).replace(",", "."))
            if m:
                try:
                    snap.values[sid] = (float(m.group(1)), m.group(2).strip())
                    hw, grp, name = (["", "", ""] + tr)[-3:]
                    snap.entries.append((hw, grp, name, sid))
                    snap.labels[sid] = " / ".join(x for x in tr[-3:] if x)
                except ValueError:
                    pass
        for c in n.get("Children", []):
            walk(c, tr)
    walk(data, [])
    return snap


def source_catalog(snap):
    out = [{"id": k, "label": k} for k in SOURCES] + [{"id": k, "label": k} for k in TIME_SOURCES]
    out += [{"id": "lhm:" + sid, "label": "LHM: " + snap.labels[sid]} for sid in sorted(snap.labels)]
    return out


class History:
    """per-source ring buffers of (time, value) for graph widgets"""
    def __init__(self):
        self.data = {}
        self.keep = {}
        self.lock = threading.Lock()

    def want(self, source, seconds):
        self.keep[source] = max(self.keep.get(source, 0), float(seconds))

    def sample(self, snap):
        now = snap.time
        with self.lock:
            for src, secs in self.keep.items():
                v, _ = snap.value(src)
                if v is None:
                    continue
                dq = self.data.setdefault(src, collections.deque())
                dq.append((now, v))
                while dq and now - dq[0][0] > secs + 5:
                    dq.popleft()

    def series(self, source, seconds, now):
        with self.lock:
            return [(t, v) for t, v in self.data.get(source, ()) if now - t <= seconds]


HISTORY = History()


# ============================ helpers ============================
def parse_color(s, default=(255, 255, 255, 255)):
    if not s:
        return default
    s = str(s).strip()
    m = re.match(r"rgba?\(([^)]+)\)", s)
    if m:
        p = [x.strip() for x in m.group(1).split(",")]
        r, g, b = (int(float(p[i])) for i in range(3))
        a = int(round(float(p[3]) * 255)) if len(p) > 3 else 255
        return (r, g, b, a)
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        a = int(h[6:8], 16) if len(h) >= 8 else 255
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)
    return default


def lerp_color(c0, c1, t):
    return tuple(int(round(c0[k] + (c1[k] - c0[k]) * t)) for k in range(4))


@lru_cache(maxsize=256)
def get_font(family, bold, size):
    size = max(6, int(round(size)))
    path = fonts.resolve(family, bold) or fonts.resolve("Segoe UI", bold)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


@lru_cache(maxsize=4)
def _load_background(path, mtime, w, h):
    try:
        return Image.open(path).convert("RGBA").resize((w, h), Image.LANCZOS)
    except Exception:
        return Image.new("RGBA", (w, h), (0, 0, 0, 255))


def paste(base, img, x, y):
    """alpha-composite img onto base at (x, y) with clipping"""
    x, y = int(round(x)), int(round(y))
    bw, bh = base.size
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(img.width, bw - x), min(img.height, bh - y)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    base.alpha_composite(img, dest=(x + sx0, y + sy0), source=(sx0, sy0, sx1, sy1))


def fmt_value(v, unit, decimals=0, show_unit=True):
    if v is None:
        return "--" + (unit if show_unit else "")
    return f"{v:.{int(decimals)}f}" + (unit if show_unit else "")


def pick_range_color(value, ranges, default):
    col = default
    if value is None or not ranges:
        return col
    for r in sorted(ranges, key=lambda r: float(r.get("from", 0))):
        if value >= float(r.get("from", 0)):
            col = r.get("color", col)
    return col


def font_of(w, key="font", size=20):
    f = w.get(key) or {}
    return f.get("family") or "Segoe UI", bool(f.get("bold")), float(f.get("size", size))


# ============================ theme ============================
class Theme:
    def __init__(self, data, base_dir=""):
        if data.get("version") != FORMAT_VERSION:
            data = convert_darkflash(data)
        self.t = data
        self.base_dir = base_dir
        self.widgets = data.setdefault("widgets", [])
        self._static_key = None
        self._static_img = None
        self._grad_cache = {}
        for w in self.widgets:
            if w.get("type") == "graph" and w.get("source"):
                HISTORY.want(w["source"], w.get("seconds", 120))

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8-sig") as f:
            return cls(json.load(f), os.path.dirname(os.path.abspath(path)))

    def background_path(self):
        p = (self.t.get("background") or {}).get("path", "")
        return p if not p or os.path.isabs(p) else os.path.join(self.base_dir, p)

    # ---------- change detection ----------
    @staticmethod
    def _is_static(w):
        return w.get("type") == "text"

    def signature(self, snap, now=None):
        """cheap tuple that changes whenever the rendered picture would change"""
        now = now or datetime.datetime.now()
        sig = []
        for w in self.widgets:
            t = w.get("type")
            if t == "text":
                continue
            src = w.get("source", "")
            if src in TIME_SOURCES:
                sig.append(now.strftime(TIME_SOURCES[src])); continue
            v, unit = snap.value(src)
            if t == "value":
                sig.append(fmt_value(v, unit, w.get("decimals", 0), w.get("unit", True)))
            elif t == "bar":
                lo, hi = float(w.get("min", 0)), float(w.get("max", 100))
                frac = 0 if v is None or hi <= lo else max(0, min(1, (v - lo) / (hi - lo)))
                sig.append(int(frac * float(w.get("w", 100))))
            elif t in ("ring", "gauge"):
                lo, hi = float(w.get("min", 0)), float(w.get("max", 100))
                frac = 0 if v is None or hi <= lo else max(0, min(1, (v - lo) / (hi - lo)))
                sig.append((int(frac * 360), fmt_value(v, unit, w.get("decimals", 0), w.get("unit", True))))
            elif t == "graph":
                s = HISTORY.series(src, float(w.get("seconds", 120)), snap.time)
                sig.append((len(s), round(s[-1][1], 1) if s else None, int(snap.time // max(1, float(w.get("seconds", 120)) / max(1, float(w.get("w", 200)))))))
        return tuple(sig)

    # ---------- render ----------
    def render(self, snap, now=None, boxes=None, scale=1.0):
        now = now or datetime.datetime.now()
        k = float(scale)
        size = (int(W * k), int(H * k))
        img = self._static_layer(size, k).copy()
        static_prefix = self._static_prefix_len()
        for i, w in enumerate(self.widgets):
            if w.get("hidden"):
                continue
            if i < static_prefix:
                if boxes is not None:
                    boxes.append(self._text_box(w))
                continue
            try:
                box = DRAW[w.get("type")](self, img, w, snap, now, k)
            except KeyError:
                box = None
            if boxes is not None and box:
                boxes.append({"id": w.get("id"), "x": box[0], "y": box[1], "w": box[2], "h": box[3]})
        return img.convert("RGB")

    def _static_prefix_len(self):
        n = 0
        for w in self.widgets:
            if self._is_static(w) or w.get("hidden"):
                n += 1
            else:
                break
        return n

    def _static_layer(self, size, k):
        bg = self.background_path()
        mtime = os.path.getmtime(bg) if bg and os.path.exists(bg) else 0
        prefix = self.widgets[: self._static_prefix_len()]
        key = (bg, mtime, size, json.dumps(prefix, sort_keys=True))
        if key != self._static_key:
            base = _load_background(bg, mtime, size[0], size[1]).copy()
            for w in prefix:
                if not w.get("hidden"):
                    draw_text(self, base, w, None, None, k)
            self._static_key, self._static_img = key, base
        return self._static_img

    def _text_box(self, w):
        family, bold, fs = font_of(w)
        font = get_font(family, bold, fs)
        bb = font.getbbox(str(w.get("content", "")) or " ")
        return {"id": w.get("id"), "x": w.get("x", 0), "y": w.get("y", 0), "w": max(8, bb[2]), "h": max(fs, 8)}

    def gradient_strip(self, key, width, height, c0, c1):
        ck = (key, width, height, c0, c1)
        g = self._grad_cache.get(ck)
        if g is None:
            g = Image.new("RGBA", (max(1, width), max(1, height)))
            d = ImageDraw.Draw(g)
            for i in range(width):
                d.line([(i, 0), (i, height)], fill=lerp_color(c0, c1, i / max(1, width - 1)))
            if len(self._grad_cache) > 64:
                self._grad_cache.clear()
            self._grad_cache[ck] = g
        return g


# ============================ widgets ============================
def _card(base, w, x, y, bw, bh, k):
    c = w.get("card") or {}
    if not c.get("enabled"):
        return
    pad = float(c.get("padding", 6)) * k
    r = float(c.get("radius", 8)) * k
    cw, ch = int(bw + 2 * pad), int(bh + 2 * pad)
    if cw < 2 or ch < 2:
        return
    layer = Image.new("RGBA", (cw * SS, ch * SS), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([0, 0, cw * SS - 1, ch * SS - 1], radius=r * SS, fill=parse_color(c.get("color"), (0, 0, 0, 110)))
    paste(base, layer.resize((cw, ch), Image.LANCZOS), x - pad, y - pad)


def _text_image(txt, family, bold, fs, color):
    """-> (image, dx, dy, text width). Paste at (x+dx, y+dy); glyphs sit where a full-canvas draw at (x, y) would put them."""
    font = get_font(family, bold, fs)
    oy = -font.getbbox("0")[1] + fs * 0.12
    bb = font.getbbox(txt or " ")
    x0, y0 = min(0, bb[0]) - 2, math.floor(min(0.0, oy + bb[1])) - 2
    x1, y1 = bb[2] + 2, max(oy + bb[3], 1.0) + 2
    im = Image.new("RGBA", (int(math.ceil(x1 - x0)) + 2, int(math.ceil(y1 - y0)) + 2), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((-x0, oy - y0), txt, font=font, fill=color)
    return im, x0, y0, bb[2]


def draw_text(theme, base, w, snap, now, k):
    family, bold, fs = font_of(w)
    txt = str(w.get("content", ""))
    im, dx, dy, tw = _text_image(txt, family, bold, fs * k, parse_color(w.get("color")))
    x, y = float(w.get("x", 0)) * k, float(w.get("y", 0)) * k
    _card(base, w, x, y, tw, fs * k, k)
    paste(base, im, x + dx, y + dy)
    return (float(w.get("x", 0)), float(w.get("y", 0)), max(8, tw / k), max(8, fs))


def draw_value(theme, base, w, snap, now, k):
    src = w.get("source", "")
    if src in TIME_SOURCES:
        txt, v = now.strftime(TIME_SOURCES[src]), None
    else:
        v, unit = snap.value(src)
        txt = fmt_value(v, unit, w.get("decimals", 0), w.get("unit", True))
    color = pick_range_color(v, w.get("colorRanges"), w.get("color"))
    family, bold, fs = font_of(w)
    im, dx, dy, tw = _text_image(txt, family, bold, fs * k, parse_color(color))
    x, y = float(w.get("x", 0)) * k, float(w.get("y", 0)) * k
    _card(base, w, x, y, tw, fs * k, k)
    paste(base, im, x + dx, y + dy)
    return (float(w.get("x", 0)), float(w.get("y", 0)), max(8, tw / k), max(8, fs))


def _fraction(w, snap):
    v, unit = snap.value(w.get("source", ""))
    lo, hi = float(w.get("min", 0)), float(w.get("max", 100))
    frac = 0.0 if v is None or hi <= lo else max(0.0, min(1.0, (v - lo) / (hi - lo)))
    return v, unit, frac


def draw_bar(theme, base, w, snap, now, k):
    x, y = float(w.get("x", 0)) * k, float(w.get("y", 0)) * k
    bw, bh = max(1, int(float(w.get("w", 100)) * k)), max(1, int(float(w.get("h", 20)) * k))
    v, unit, frac = _fraction(w, snap)
    _card(base, w, x, y, bw, bh, k)
    radius = float(w.get("radius", 0)) * k
    c0, c1 = parse_color(w.get("colorStart"), (19, 187, 206, 255)), parse_color(w.get("colorEnd"), (206, 19, 19, 255))
    bg = parse_color(w.get("bg"), (204, 204, 204, 255))
    segs = int(w.get("segments", 0) or 0)
    layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if segs > 1:
        gap = float(w.get("gap", 2)) * k
        sw = (bw - gap * (segs - 1)) / segs
        lit = int(round(frac * segs))
        for i in range(segs):
            sx = i * (sw + gap)
            col = lerp_color(c0, c1, i / max(1, segs - 1)) if i < lit else bg
            d.rounded_rectangle([sx, 0, sx + sw - 1, bh - 1], radius=min(radius, sw / 2, bh / 2), fill=col)
    else:
        d.rounded_rectangle([0, 0, bw - 1, bh - 1], radius=min(radius, bh / 2), fill=bg)
        fw = int(bw * frac)
        if fw > 0:
            grad = theme.gradient_strip(w.get("id"), bw, bh, c0, c1).crop((0, 0, fw, bh))
            if radius > 0:
                mask = Image.new("L", (bw, bh), 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=min(radius, bh / 2), fill=255)
                grad = Image.composite(grad, Image.new("RGBA", grad.size, (0, 0, 0, 0)), mask.crop((0, 0, fw, bh)))
            layer.alpha_composite(grad, dest=(0, 0))
    paste(base, layer, x, y)
    return (float(w.get("x", 0)), float(w.get("y", 0)), bw / k, bh / k)


def _arc_gradient(d, box, start, sweep, frac, width, c0, c1, step=3.0):
    """draw gradient arc from start over sweep*frac degrees (PIL angles: 0 = 3 o'clock, clockwise)"""
    end = sweep * frac
    a = 0.0
    while a < end - 1e-6:
        b = min(end, a + step)
        col = lerp_color(c0, c1, (a + b) / 2 / max(1e-6, sweep))
        d.arc(box, start + a, start + b + 0.6, fill=col, width=width)
        a = b


def _center_texts(base, w, cx, cy, v, unit, k, value_dy=0.0):
    if w.get("showValue", True):
        color = pick_range_color(v, w.get("colorRanges"), w.get("color"))
        family, bold, fs = font_of(w, "font", 28)
        txt = fmt_value(v, unit, w.get("decimals", 0), w.get("unit", True))
        im, dx, dy, tw = _text_image(txt, family, bold, fs * k, parse_color(color))
        paste(base, im, cx - tw / 2 + dx, cy - fs * k * 0.5 + value_dy * k + dy)
    if w.get("label"):
        family, bold, fs = font_of(w, "labelFont", 14)
        im, dx, dy, tw = _text_image(str(w["label"]), family, bold, fs * k, parse_color(w.get("labelColor"), (200, 200, 200, 255)))
        paste(base, im, cx - tw / 2 + dx, cy + float(font_of(w, "font", 28)[2]) * k * 0.55 + value_dy * k + dy)


def draw_ring(theme, base, w, snap, now, k):
    size = float(w.get("size", 120))
    x, y = float(w.get("x", 0)) * k, float(w.get("y", 0)) * k
    S = max(4, int(size * k))
    v, unit, frac = _fraction(w, snap)
    _card(base, w, x, y, S, S, k)
    th = max(1, int(float(w.get("thickness", 12)) * k * SS))
    start, sweep = float(w.get("startAngle", 135)), float(w.get("sweep", 270))
    layer = Image.new("RGBA", (S * SS, S * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    box = [th / 2, th / 2, S * SS - th / 2 - 1, S * SS - th / 2 - 1]
    bg = parse_color(w.get("bg"), (255, 255, 255, 40))
    c0, c1 = parse_color(w.get("colorStart"), (19, 187, 206, 255)), parse_color(w.get("colorEnd"), (206, 19, 19, 255))
    d.arc(box, start, start + sweep, fill=bg, width=th)
    _arc_gradient(d, box, start, sweep, frac, th, c0, c1)
    if w.get("roundCap", True) and frac > 0:
        rad = (S * SS - th) / 2
        c = S * SS / 2
        for ang, col in ((start, c0), (start + sweep * frac, lerp_color(c0, c1, frac))):
            px, py = c + rad * math.cos(math.radians(ang)), c + rad * math.sin(math.radians(ang))
            d.ellipse([px - th / 2, py - th / 2, px + th / 2, py + th / 2], fill=col)
    paste(base, layer.resize((S, S), Image.LANCZOS), x, y)
    _center_texts(base, w, x + S / 2, y + S / 2, v, unit, k)
    return (float(w.get("x", 0)), float(w.get("y", 0)), size, size)


def draw_gauge(theme, base, w, snap, now, k):
    size = float(w.get("size", 160))                      # width; height = size/2 + text room
    x, y = float(w.get("x", 0)) * k, float(w.get("y", 0)) * k
    S = max(4, int(size * k))
    Hh = int(S / 2 + S * 0.28)
    v, unit, frac = _fraction(w, snap)
    _card(base, w, x, y, S, Hh, k)
    th = max(1, int(float(w.get("thickness", 14)) * k * SS))
    layer = Image.new("RGBA", (S * SS, S * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    box = [th / 2, th / 2, S * SS - th / 2 - 1, S * SS - th / 2 - 1]
    bg = parse_color(w.get("bg"), (255, 255, 255, 40))
    c0, c1 = parse_color(w.get("colorStart"), (19, 187, 206, 255)), parse_color(w.get("colorEnd"), (206, 19, 19, 255))
    d.arc(box, 180, 360, fill=bg, width=th)
    _arc_gradient(d, box, 180, 180, frac, th, c0, c1)
    c = S * SS / 2
    rad = (S * SS - th) / 2
    ticks = int(w.get("ticks", 10) or 0)
    tick_col = parse_color(w.get("tickColor"), (255, 255, 255, 150))
    for i in range(ticks + 1 if ticks > 0 else 0):
        ang = math.radians(180 + 180 * i / ticks)
        r0, r1 = rad - th * 0.9, rad - th * (1.6 if i % 5 == 0 else 1.25)
        d.line([(c + r0 * math.cos(ang), c + r0 * math.sin(ang)), (c + r1 * math.cos(ang), c + r1 * math.sin(ang))], fill=tick_col, width=max(1, int(k * SS * 1.5)))
    ang = math.radians(180 + 180 * frac)
    needle = parse_color(w.get("needleColor"), (255, 255, 255, 230))
    nl = rad - th * 0.4
    d.line([(c, c), (c + nl * math.cos(ang), c + nl * math.sin(ang))], fill=needle, width=max(2, int(3 * k * SS)))
    hub = max(3, th * 0.45)
    d.ellipse([c - hub, c - hub, c + hub, c + hub], fill=needle)
    half = layer.crop((0, 0, S * SS, int(S * SS / 2 + hub + SS))).resize((S, int(S / 2 + hub / SS + 1)), Image.LANCZOS)
    paste(base, half, x, y)
    _center_texts(base, w, x + S / 2, y + S / 2, v, unit, k, value_dy=float(font_of(w, "font", 22)[2]) * 0.75)
    return (float(w.get("x", 0)), float(w.get("y", 0)), size, Hh / k)


def draw_graph(theme, base, w, snap, now, k):
    x, y = float(w.get("x", 0)) * k, float(w.get("y", 0)) * k
    gw, gh = max(4, int(float(w.get("w", 240)) * k)), max(4, int(float(w.get("h", 80)) * k))
    _card(base, w, x, y, gw, gh, k)
    secs = float(w.get("seconds", 120))
    series = HISTORY.series(w.get("source", ""), secs, snap.time)
    layer = Image.new("RGBA", (gw * SS, gh * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if w.get("grid", True):
        gc = parse_color(w.get("gridColor"), (255, 255, 255, 35))
        for i in range(1, 4):
            gy = gh * SS * i / 4
            d.line([(0, gy), (gw * SS, gy)], fill=gc, width=max(1, SS))
    if len(series) >= 2:
        lo = float(w["min"]) if w.get("min") is not None else 0.0
        hi = w.get("max")
        hi = float(hi) if hi is not None else max(v for _, v in series) * 1.1 or 1.0
        hi = hi if hi > lo else lo + 1
        pts = []
        for t, v in series:
            px = (1 - (snap.time - t) / secs) * gw * SS
            py = (1 - max(0.0, min(1.0, (v - lo) / (hi - lo)))) * (gh * SS - 1)
            pts.append((px, py))
        if w.get("style", "area") == "area":
            fill = parse_color(w.get("fillColor"), (63, 182, 255, 80))
            d.polygon([(pts[0][0], gh * SS)] + pts + [(pts[-1][0], gh * SS)], fill=fill)
        d.line(pts, fill=parse_color(w.get("lineColor"), (63, 182, 255, 255)), width=max(1, int(float(w.get("lineWidth", 2)) * k * SS)), joint="curve")
    paste(base, layer.resize((gw, gh), Image.LANCZOS), x, y)
    return (float(w.get("x", 0)), float(w.get("y", 0)), gw / k, gh / k)


DRAW = {"text": draw_text, "value": draw_value, "bar": draw_bar, "ring": draw_ring, "gauge": draw_gauge, "graph": draw_graph}


# ============================ DarkFlash v1 -> v2 ============================
def convert_darkflash(t):
    widgets = (t.get("osd") or {}).get("widget", [])
    cw = None
    for w in widgets:
        cp = (w.get("property") or [{}])[0].get("canvasProperties")
        if cp:
            cw = float(cp["width"]); break
    s = W / (cw or 571.4285714285714)
    order = sorted(widgets, key=lambda w: (w.get("layerIndex", 0), 0 if w.get("type") == "bar" else 1))
    out = []
    for w in order:
        p = (w.get("property") or [{}])[0]
        typ = w.get("type")
        base = {"id": w.get("id")}
        if typ in ("text", "data"):
            col = parse_color(p.get("textColor"))
            col = col[:3] + (col[3] * float(p.get("opacity", 1)) / 255,)
            nw = dict(base, type="text" if typ == "text" else "value",
                      x=round((float(p.get("x", 0)) + 2) * s, 1), y=round(float(p.get("y", 0)) * s, 1),
                      font={"family": (p.get("fontFamily") or "Segoe UI").strip('"\' '), "size": round(float(p.get("fontSize", 16)) * s, 2), "bold": bool(p.get("bold"))},
                      color=f"rgba({col[0]}, {col[1]}, {col[2]}, {round(col[3], 3)})")
            if typ == "text":
                nw["content"] = p.get("content", "")
            else:
                nw.update(source=p.get("source", ""), decimals=0, unit=True)
            out.append(nw)
        elif typ == "bar":
            out.append(dict(base, type="bar", x=round(float(p.get("x", 0)) * s, 1), y=round(float(p.get("y", 0)) * s, 1),
                            w=round(float(p.get("width", 80)) * s, 1), h=round(float(p.get("height", 20)) * s, 1),
                            source=p.get("source", ""), min=p.get("minValue", 0), max=p.get("maxValue", 100),
                            bg=p.get("bgColor", "#cccccc"), colorStart=p.get("startColor"), colorEnd=p.get("endColor"),
                            segments=0, gap=2, radius=round(float(p.get("cornerRadius", 0)) * s, 1)))
    bg = t.get("background") or {}
    return {"version": FORMAT_VERSION, "background": {"path": bg.get("path", "")}, "widgets": out}

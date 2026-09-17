"""Installed font lookup (Windows registry) -> family name -> {regular, bold} file paths."""
import os, re, winreg
from functools import lru_cache

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def _read(root, key, base):
    out = {}
    try:
        with winreg.OpenKey(root, key) as k:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                if not isinstance(val, str) or not val.lower().endswith((".ttf", ".otf", ".ttc")):
                    continue
                path = val if os.path.isabs(val) else os.path.join(base, val)
                out[name] = path
    except OSError:
        pass
    return out


@lru_cache(maxsize=1)
def families():
    entries = {}
    entries.update(_read(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts", FONT_DIR))
    entries.update(_read(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
                         os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Windows\Fonts")))
    fam = {}
    for name, path in entries.items():
        n = re.sub(r"\s*\((TrueType|OpenType)\)\s*$", "", name).split(" & ")[0].strip()
        bold = bool(re.search(r"\bBold\b", n)) and not re.search(r"Semi|Demi|Extra|Ultra", n)
        italic = bool(re.search(r"\b(Italic|Oblique)\b", n))
        base = re.sub(r"\s+(Bold|Italic|Oblique|Regular)\b", "", n).strip()
        if italic:
            continue
        if not re.fullmatch(r"[A-Za-z0-9 \-]+", base) and not re.search(r"[가-힣]", base):
            continue
        d = fam.setdefault(base, {})
        if not os.path.exists(path):
            continue
        d["bold" if bold else "regular"] = d.get("bold" if bold else "regular") or path
    return {k: v for k, v in fam.items() if "regular" in v}


def resolve(family, bold):
    fams = families()
    key = (family or "").strip('"\' ')
    for k, v in fams.items():
        if k.lower() == key.lower():
            return v.get("bold") if bold and v.get("bold") else v["regular"]
    return None


def names():
    return sorted(families().keys(), key=str.lower)

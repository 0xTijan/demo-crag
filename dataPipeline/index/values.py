"""Spec values in the full value format (plan v1, section 2 and section 13 rule 1).

Every extracted value is a dict with the same fields, whatever produced it:

    {"value": 60, "unit": "V", "stat": "max", "conditions": {"ta": "25 °C"}, "page": 1, "raw": "60"}
    {"min": -55, "max": 150, "unit": "degC", "stat": "range", ...}          a range
    {"set": [1, 2, 5, 10], "unit": "%", ...}                                options of a family row
    {"value": "TO-92", "page": 4, "raw": "TO-92"}                           a text value

plus `"verified": false` and `"issues": [...]` when a check in code failed (the value is kept, but shown
with a warning). This module turns the LLM's compact line into that dict (`parse_line`), converts units to
the spec list's unit (`convert`) and runs the two checks of the plan (`check_value`): the raw text is on the
cited page, and min <= max.
"""
from __future__ import annotations

import re

# ------------------------------------------------------------------ units
PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3,
          "M": 1e6, "G": 1e9}
UNIT_ALIASES = {"ω": "ohm", "Ω": "ohm", "ohms": "ohm", "°c": "degC", "℃": "degC", "degc": "degC", "deg c": "degC",
                "c": "degC", "°": "deg", "volt": "V", "volts": "V", "amp": "A", "amps": "A", "watt": "W",
                "hertz": "Hz", "hz": "Hz", "sec": "s"}


def norm_unit(unit) -> str:
    """'mΩ' -> 'mohm', '°C' -> 'degC', 'µA' -> 'uA', ' V ' -> 'V'."""
    u = " ".join(str(unit or "").split())
    if not u:
        return ""
    u = u.replace("µ", "u").replace("μ", "u").replace("Ω", "ohm").replace("ω", "ohm").replace("℃", "degC")
    u = u.replace("°C", "degC").replace("°c", "degC")
    return UNIT_ALIASES.get(u.lower(), UNIT_ALIASES.get(u, u))


def factor(unit: str, target: str) -> float | None:
    """Multiplier from `unit` to `target` ('mA' -> 'A' = 0.001), None if they do not convert."""
    unit, target = norm_unit(unit), norm_unit(target)
    if not unit or not target or unit == target:
        return 1.0
    if unit.lower() == target.lower() and len(unit) > 1 and unit.lower() not in ("ma", "mv", "mw"):
        return 1.0  # 'hz' vs 'Hz', 'OHM' vs 'ohm'
    for base_len in range(len(target), 0, -1):  # target may carry a prefix itself (pF, mohm)
        tp, base = target[:-base_len], target[-base_len:]
        if tp not in PREFIX or not unit.endswith(base):
            continue
        up = unit[: -base_len] if base_len else unit
        if up in PREFIX and (up + base) == unit:
            return PREFIX[up] / PREFIX[tp]
    return None


def convert(number: float, unit: str, target: str) -> tuple[float, bool]:
    """(number in target unit, converted ok)."""
    f = factor(unit, target)
    if f is None:
        return number, False
    return clean_number(number * f), True


def clean_number(x: float) -> float | int:
    """Round away float noise (0.1 * 1e-9 -> 1e-10) and give ints as int."""
    if x == 0:
        return 0
    y = float(f"{x:.10g}")
    return int(y) if y.is_integer() and abs(y) < 1e15 else y


# ------------------------------------------------------------------ numbers
NUM = r"[+\-−]?\s*(?:\d+(?:[.,]\d+)?|\.\d+)(?:[eE][+\-]?\d+)?"
NUM_RE = re.compile(rf"^(?:±|\+/-)?\s*({NUM})\s*([pnuµμmkKMG]?)$")
RANGE_SEP = r"\s*(?:\.\.|…|~|–|—|\bto\b|\bthru\b|-)\s*"
RANGE_RE = re.compile(rf"^({NUM}\s*[pnuµμmkKMG]?){RANGE_SEP}({NUM}\s*[pnuµμmkKMG]?)$")


def parse_number(text: str) -> float | None:
    """'60' -> 60, '-55' -> -55, '0,5' -> 0.5, '±10' -> 10, '470n' -> 4.7e-07, '2.2k' -> 2200; else None."""
    t = str(text).strip().replace("−", "-")
    m = NUM_RE.match(t)
    if not m:
        return None
    num = m.group(1).replace(" ", "")
    if "," in num and "." not in num:
        num = num.replace(",", ".")
    try:
        x = float(num)
    except ValueError:
        return None
    return clean_number(x * PREFIX[m.group(2)])


def parse_value(text: str, numeric: bool = True) -> dict:
    """The value field of a compact line -> {"value": x} | {"min": a, "max": b} | {"set": [...]}.

    '60' | '-55..150' | '-55 to 150' | '150p..470n' | '1; 2; 5; 10' | 'TO-92'
    numeric=False (a spec without a unit: package, dielectric, marking) keeps text as text ('0603')."""
    t = " ".join(str(text or "").split())
    if not t:
        return {}
    if ";" in t:
        items = [x.strip() for x in t.split(";") if x.strip()]
        out = []
        for x in items:
            n = parse_number(x) if numeric else None
            out.append(n if n is not None else x)
        return {"set": out}
    if not numeric:
        return {"value": t}
    n = parse_number(t)
    if n is not None:
        return {"value": n}
    m = RANGE_RE.match(t.replace("−", "-"))
    if m:
        a, b = parse_number(m.group(1)), parse_number(m.group(2))
        if a is not None and b is not None:
            return {"min": a, "max": b}
    return {"value": t}


def parse_conditions(text: str) -> dict:
    """'VGS=10V, ID=500mA' -> {"vgs": "10V", "id": "500mA"}; other text -> {"text": ...}; '' -> {}."""
    t = " ".join(str(text or "").split())
    if not t:
        return {}
    parts = [p.strip() for p in re.split(r"[,;]\s*", t) if p.strip()]
    out = {}
    for p in parts:
        k, sep, v = p.partition("=")
        if not sep or not k.strip() or not v.strip():
            return {"text": t}
        out[re.sub(r"[^a-z0-9]+", "_", k.strip().lower()).strip("_") or "x"] = v.strip()
    return out


# ------------------------------------------------------------------ compact lines
def split_line(line: str) -> list[str]:
    return [p.strip() for p in str(line).split("|")]


def parse_line(line: str, spec: dict) -> tuple[str, dict] | None:
    """'vds_max | 60 | V | 1 | 60 | Tj=25C' (key | value | unit | page | raw | conditions) -> (key, value dict)
    in the spec list's unit. None for an empty or broken line."""
    parts = split_line(line) + [""] * 6
    key, value, unit, page, raw, cond = parts[:6]
    if not key or not value or value.lower() in ("-", "n/a", "na", "none", "null", "?"):
        return None
    target = (spec or {}).get("unit") or ""
    out = parse_value(value, numeric=bool(target) or (spec or {}).get("rule") in (">=", "<=", "≈", "contains"))
    if not out:
        return None
    issues = []
    numeric = [k for k in ("value", "min", "max") if isinstance(out.get(k), (int, float))]
    if "set" in out:
        if any(isinstance(x, (int, float)) for x in out["set"]) and target:
            conv = []
            for x in out["set"]:
                if isinstance(x, (int, float)):
                    y, ok = convert(x, unit, target)
                    conv.append(y)
                    if not ok:
                        issues.append(f"unit {unit!r} does not convert to {target!r}")
                else:
                    conv.append(x)
            out["set"] = conv
    elif numeric and target:
        for k in numeric:
            out[k], ok = convert(out[k], unit, target)
            if not ok:
                issues.append(f"unit {unit!r} does not convert to {target!r}")
                break
    if target and (numeric or "set" in out):
        out["unit"] = target if not issues else norm_unit(unit)
    elif unit and not target:
        out["unit"] = norm_unit(unit)
    stat = (spec or {}).get("stat")
    if stat:
        out["stat"] = "range" if ("min" in out and stat in ("value", "range")) else stat
    out["conditions"] = parse_conditions(cond) if cond else {}
    try:
        out["page"] = int(re.sub(r"\D", "", page)) if re.search(r"\d", page) else None
    except ValueError:
        out["page"] = None
    out["raw"] = raw or value
    if issues:
        out["issues"] = sorted(set(issues))
    return key.lower(), out


# ------------------------------------------------------------------ checks (plan section 2)
def _norm_text(text: str) -> str:
    t = str(text).lower().replace("−", "-").replace("–", "-").replace("—", "-").replace("µ", "u").replace("μ", "u")
    t = t.replace("ω", "ohm").replace("℃", "°c").replace("\\", "")
    return re.sub(r"[\s|*_]+", "", t)


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)?", str(text))


def raw_on_page(raw: str, page_text: str) -> bool:
    """The raw text is on the page. Raw text without letters ("60", "-55 to +150", "225~1.6"): every number in
    it is a whole number on the page ("5" is not found inside "-55"). Short text ("K", "B"): a whole word.
    Other text: anywhere, ignoring case, spaces and table bars."""
    raw = str(raw or "").strip()
    if not raw:
        return False
    nums = _numbers(raw)
    if not re.search(r"[a-zA-Zµμω°Ω]", raw):
        page_nums = {n.replace(",", ".") for n in _numbers(page_text)}
        return bool(nums) and all(n.replace(",", ".") in page_nums for n in nums)
    if len(_norm_text(raw)) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(raw.lower())}(?![a-z0-9])", page_text.lower()) is not None
    return _norm_text(raw) in _norm_text(page_text)


def check_value(v: dict, pages: dict[int, str]) -> dict:
    """Check 1: `raw` is on the cited page (if it is on another page, the page is corrected).
    Check 2: min <= max. Sets verified / issues on the value dict and returns it."""
    issues = list(v.get("issues") or [])
    page = v.get("page")
    if page in pages and raw_on_page(v.get("raw", ""), pages[page]):
        pass
    else:
        other = next((n for n in sorted(pages) if raw_on_page(v.get("raw", ""), pages[n])), None)
        if other is not None:
            if page is not None:
                v["page_given"] = page
            v["page"] = other
        else:
            issues.append("raw text not found on the page" if page in pages else
                          "raw text not found in the datasheet" if page is None else f"page {page} does not exist")
    if isinstance(v.get("min"), (int, float)) and isinstance(v.get("max"), (int, float)) and v["min"] > v["max"]:
        issues.append("min > max")
    if issues:
        v["verified"] = False
        v["issues"] = sorted(set(issues))
    else:
        v.pop("issues", None)
        v["verified"] = True
    return v


def show(v: dict) -> str:
    """Short text of a value dict for printing: '60 V', '-55..150 degC', '{1, 2, 5} %', 'TO-92'."""
    unit = f" {v['unit']}" if v.get("unit") else ""
    if "set" in v:
        return "{" + ", ".join(str(x) for x in v["set"]) + "}" + unit
    if "min" in v or "max" in v:
        return f"{v.get('min', '')}..{v.get('max', '')}{unit}"
    return f"{v.get('value', '')}{unit}"

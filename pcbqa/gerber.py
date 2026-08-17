"""Independent Gerber X2 and Excellon readers.

This exists so the exported fabrication geometry can be checked on its own
terms rather than trusted because the native board was fine. It is deliberately
fail-closed: an aperture, macro primitive or command it does not understand
raises, because silently skipping apertures is exactly the failure that let a
via-in-pad population through an earlier review.
"""

from __future__ import annotations

import math
import os
import re

try:
    from shapely.geometry import Polygon, LineString, Point
    from shapely.ops import unary_union
except ImportError as exc:                      # pragma: no cover
    raise RuntimeError("shapely is required for Gerber verification") from exc


class GerberError(Exception):
    pass


# ---------------------------------------------------------------------------
# aperture macro interpreter
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"\$\d+|\d*\.?\d+|[-+xX/()]")


def _eval_expr(expr, args):
    """Evaluate a Gerber macro arithmetic expression ($1, +, -, x, /, parens)."""
    text = expr.strip().replace("X", "*").replace("x", "*")
    text = re.sub(r"\$(\d+)", lambda m: repr(float(args.get(int(m.group(1)), 0.0))), text)
    if not re.fullmatch(r"[0-9eE\.\+\-\*/() ]*", text):
        raise GerberError(f"unsupported macro expression {expr!r}")
    try:
        return float(eval(text, {"__builtins__": {}}, {}))   # noqa: S307 - sanitised above
    except Exception as exc:
        raise GerberError(f"cannot evaluate macro expression {expr!r}: {exc}") from exc


class ApertureMacro:
    def __init__(self, name, body):
        self.name = name
        self.body = [line for line in body if line and not line.startswith("0 ")]

    def polygon(self, args):
        """Build the macro's shape for one set of arguments."""
        params = {i + 1: v for i, v in enumerate(args)}
        adds, subs = [], []
        for raw in self.body:
            parts = [p.strip() for p in raw.split(",")]
            code = parts[0]
            if code.startswith("$"):             # variable assignment  $4=$1x2
                name, _, expr = raw.partition("=")
                params[int(name.strip()[1:])] = _eval_expr(expr, params)
                continue
            vals = [_eval_expr(p, params) for p in parts[1:]]
            shape, exposure = self._primitive(int(code), vals)
            (adds if exposure else subs).append(shape)
        if not adds:
            raise GerberError(f"macro {self.name} produced no exposed geometry")
        result = unary_union(adds)
        if subs:
            result = result.difference(unary_union(subs))
        return result

    @staticmethod
    def _primitive(code, v):
        if code == 1:                                   # circle
            exposure, diameter, cx, cy = v[0], v[1], v[2], v[3]
            rot = v[4] if len(v) > 4 else 0.0
            shape = Point(cx, cy).buffer(diameter / 2.0, quad_segs=64)
            return _rotate(shape, rot), exposure >= 0.5
        if code == 4:                                   # outline polygon
            exposure = v[0]
            count = int(v[1])
            pts = [(v[2 + 2 * i], v[3 + 2 * i]) for i in range(count + 1)]
            rot = v[2 + 2 * (count + 1)] if len(v) > 2 + 2 * (count + 1) else 0.0
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            return _rotate(poly, rot), exposure >= 0.5
        if code == 20:                                  # vector line
            exposure, width, x1, y1, x2, y2 = v[0], v[1], v[2], v[3], v[4], v[5]
            rot = v[6] if len(v) > 6 else 0.0
            shape = LineString([(x1, y1), (x2, y2)]).buffer(width / 2.0, cap_style=2)
            return _rotate(shape, rot), exposure >= 0.5
        if code == 21:                                  # centre line
            exposure, w, h, cx, cy = v[0], v[1], v[2], v[3], v[4]
            rot = v[5] if len(v) > 5 else 0.0
            shape = Polygon([(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                             (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)])
            return _rotate(shape, rot), exposure >= 0.5
        raise GerberError(f"unsupported aperture-macro primitive {code}")


def _rotate(shape, degrees):
    if abs(degrees) < 1e-12:
        return shape
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    from shapely.affinity import affine_transform
    return affine_transform(shape, [c, -s, s, c, 0, 0])


# ---------------------------------------------------------------------------
# gerber file
# ---------------------------------------------------------------------------

class GerberFile:
    """A parsed Gerber layer: X2 attributes plus resolved geometry."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.attributes = {}
        self.apertures = {}
        self.macros = {}
        self.shapes = []              # (polygon, polarity_dark)
        self.flashes = []             # (aperture_code, x, y, polygon) - per object
        self.draw_count = 0
        self.flash_count = 0
        self.region_count = 0
        self._parse()

    # -- public ------------------------------------------------------------
    @property
    def file_function(self):
        return self.attributes.get("TF.FileFunction")

    @property
    def file_polarity(self):
        return self.attributes.get("TF.FilePolarity")

    def circular_flashes(self):
        """Flashes made with a round aperture, as (x, y, diameter, polygon)."""
        out = []
        for code, x, y, shape in self.flashes:
            kind, dims = self.apertures.get(code, (None, None))
            if kind == "C":
                out.append((x, y, dims[0], shape))
        return out

    def is_empty(self):
        return not self.shapes

    def union(self):
        dark = [s for s, d in self.shapes if d]
        clear = [s for s, d in self.shapes if not d]
        if not dark:
            return None
        merged = unary_union(dark)
        if clear:
            merged = merged.difference(unary_union(clear))
        return merged

    # -- parsing -----------------------------------------------------------
    def _parse(self):
        with open(self.path, "r", encoding="utf-8", errors="strict") as fh:
            text = fh.read()

        # X2 attributes
        for m in re.finditer(r"%(T[FAO])\.?([^*%]*)\*%", text):
            body = m.group(2)
            key, _, rest = body.partition(",")
            self.attributes[f"{m.group(1)}.{key}"] = rest

        unit_mm = True
        if "%MOIN*%" in text:
            unit_mm = False
        fs = re.search(r"%FSLAX(\d)(\d)Y(\d)(\d)\*%", text)
        if not fs:
            raise GerberError(f"{self.name}: no format specification")
        int_d, dec_d = int(fs.group(1)), int(fs.group(2))
        scale = 10.0 ** dec_d
        to_mm = 1.0 if unit_mm else 25.4

        # aperture macros
        for m in re.finditer(r"%AM([^*]+)\*(.*?)%", text, re.S):
            body = [b.strip() for b in m.group(2).split("*")]
            self.macros[m.group(1)] = ApertureMacro(m.group(1), body)

        # aperture definitions
        for m in re.finditer(r"%ADD(\d+)([^,*]+)(?:,([^*]*))?\*%", text):
            code = int(m.group(1))
            shape = m.group(2)
            args = [float(v) for v in m.group(3).split("X")] if m.group(3) else []
            self.apertures[code] = (shape, args)

        # command stream
        x = y = 0.0
        current = None
        polarity_dark = True
        in_region = False
        region_pts = []
        interp = 1
        quadrant = None

        def num(raw):
            return int(raw) / scale * to_mm

        for stmt in re.findall(r"[^%*]+\*|%[^%]*%", text):
            s = stmt.strip().rstrip("*")
            if s.startswith("%"):
                if s.startswith("%LP"):
                    polarity_dark = s[3] == "D"
                continue
            if not s or s.startswith("G04"):
                continue
            if s.startswith("G36"):
                in_region, region_pts = True, []
                continue
            if s.startswith("G37"):
                in_region = False
                if len(region_pts) >= 3:
                    poly = Polygon(region_pts)
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                    if not poly.is_empty:
                        self.shapes.append((poly, polarity_dark))
                        self.region_count += 1
                region_pts = []
                continue
            if s.startswith("G74"):
                quadrant = "single"; s = s[3:]
            if s.startswith("G75"):
                quadrant = "multi"; s = s[3:]
            m = re.match(r"^G0?([123])", s)
            if m:
                interp = int(m.group(1))
                s = s[m.end():]
            m = re.match(r"^D(\d{2,})$", s)
            if m and int(m.group(1)) >= 10:
                current = int(m.group(1))
                continue
            if not s:
                continue
            coords = dict(re.findall(r"([XYIJ])(-?\d+)", s))
            op = re.search(r"D0?([123])$", s)
            nx = num(coords["X"]) if "X" in coords else x
            ny = num(coords["Y"]) if "Y" in coords else y
            if op is None:
                x, y = nx, ny
                continue
            code = op.group(1)
            if code == "2":
                x, y = nx, ny
                if in_region:
                    region_pts = [(x, y)]
                continue
            if code == "1":
                if in_region:
                    region_pts.append((nx, ny))
                else:
                    if interp in (2, 3):
                        pts = self._arc(x, y, nx, ny, coords, num, interp, quadrant)
                    else:
                        pts = [(x, y), (nx, ny)]
                    self.shapes.append((self._stroke(pts, current), polarity_dark))
                    self.draw_count += 1
                x, y = nx, ny
                continue
            if code == "3":
                x, y = nx, ny
                shape = self._flash(current, x, y)
                self.shapes.append((shape, polarity_dark))
                self.flashes.append((current, x, y, shape))
                self.flash_count += 1
        if in_region:
            raise GerberError(f"{self.name}: region left open at end of file")

    def _arc(self, x0, y0, x1, y1, coords, num, interp, quadrant):
        if "I" not in coords or "J" not in coords:
            raise GerberError(f"{self.name}: arc without I/J offsets")
        cx, cy = x0 + num(coords["I"]), y0 + num(coords["J"])
        r = math.hypot(x0 - cx, y0 - cy)
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        if interp == 2:                        # clockwise
            while a1 > a0:
                a1 -= 2 * math.pi
        else:
            while a1 < a0:
                a1 += 2 * math.pi
        steps = max(8, int(abs(a1 - a0) / 0.05))
        return [(cx + r * math.cos(a0 + (a1 - a0) * i / steps),
                 cy + r * math.sin(a0 + (a1 - a0) * i / steps)) for i in range(steps + 1)]

    def _stroke(self, pts, code):
        shape, args = self._aperture(code)
        line = LineString(pts)
        if shape == "C":
            return line.buffer(args[0] / 2.0, cap_style=1, quad_segs=32)
        if shape in ("R", "O"):
            return line.buffer(min(args[0], args[1]) / 2.0, cap_style=3, quad_segs=32)
        raise GerberError(f"{self.name}: cannot stroke with aperture shape {shape}")

    def _flash(self, code, x, y):
        shape, args = self._aperture(code)
        if shape == "C":
            poly = Point(0, 0).buffer(args[0] / 2.0, quad_segs=64)
        elif shape == "R":
            w, h = args[0], args[1]
            poly = Polygon([(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)])
        elif shape == "O":
            w, h = args[0], args[1]
            r = min(w, h) / 2.0
            if w >= h:
                poly = LineString([(-(w / 2 - r), 0), (w / 2 - r, 0)]).buffer(r, quad_segs=32)
            else:
                poly = LineString([(0, -(h / 2 - r)), (0, h / 2 - r)]).buffer(r, quad_segs=32)
        elif shape == "P":
            d, n = args[0], int(args[1])
            rot = args[2] if len(args) > 2 else 0.0
            poly = Polygon([(d / 2 * math.cos(math.radians(rot + 360.0 * i / n)),
                             d / 2 * math.sin(math.radians(rot + 360.0 * i / n)))
                            for i in range(n)])
        elif shape in self.macros:
            poly = self.macros[shape].polygon(args)
        else:
            raise GerberError(f"{self.name}: unknown aperture shape {shape!r} (D{code})")
        from shapely.affinity import translate
        return translate(poly, x, y)

    def _aperture(self, code):
        if code is None or code not in self.apertures:
            raise GerberError(f"{self.name}: drawing with undefined aperture D{code}")
        return self.apertures[code]


# ---------------------------------------------------------------------------
# excellon
# ---------------------------------------------------------------------------

class ExcellonFile:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.tools = {}
        self.holes = []              # (x, y, diameter, plated)
        self.attributes = {}
        self.plated = None
        self._parse()

    def _parse(self):
        with open(self.path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.read().splitlines()
        if not lines or not lines[0].startswith("M48"):
            raise GerberError(f"{self.name}: not an Excellon file (no M48 header)")
        metric = True
        current = None
        for raw in lines:
            line = raw.strip()
            m = re.match(r"^;\s*#@!\s*(T[FAO]\.[^,]+),?(.*)$", line)
            if m:
                self.attributes[m.group(1)] = m.group(2)
                if m.group(1) == "TF.FileFunction":
                    self.plated = m.group(2).split(",")[0].lower() == "plated"
                continue
            if line.startswith(";"):
                continue
            if line in ("METRIC", "M71"):
                metric = True; continue
            if line in ("INCH", "M72"):
                metric = False; continue
            m = re.match(r"^T(\d+)C([\d.]+)", line)
            if m:
                d = float(m.group(2)) * (1.0 if metric else 25.4)
                self.tools[m.group(1)] = d
                continue
            m = re.match(r"^T(\d+)$", line)
            if m:
                current = m.group(1); continue
            m = re.match(r"^X(-?[\d.]+)Y(-?[\d.]+)$", line)
            if m and current is not None:
                k = 1.0 if metric else 25.4
                self.holes.append((float(m.group(1)) * k, float(m.group(2)) * k,
                                   self.tools.get(current), self.plated))
        if self.plated is None:
            raise GerberError(f"{self.name}: no TF.FileFunction attribute; "
                              f"plated/non-plated cannot be established")


def sniff(path):
    """What a file is, from the first thing it says. Never from its name.

    A fabrication archive is named for whoever reads it, and those names are
    conventions rather than types: the same layer ships as `.gbr` to one
    house and under a Protel extension to another. Reading the header is the
    only way to know what is in a file, and the only way this parser survives
    a change of convention - keying on one extension made every export in the
    other unreadable, which is a validator that quietly checks nothing rather
    than one that complains.
    """
    with open(path, "rb") as fh:
        head = fh.read(4096).decode("utf-8", "ignore")
    stripped = head.lstrip()
    if stripped.startswith("{"):
        return "json"                     # a Gerber job file
    for line in head.splitlines()[:40]:
        text = line.strip()
        if text.startswith("M48"):
            return "drill"
        if text.startswith(("%FS", "%MO", "%TF", "%AD", "G04", "G75", "G01")):
            return "gerber"
    return None


def load_layers(directory):
    """Parse every Gerber and drill file in a directory."""
    gerbers, drills, unparsed = {}, {}, []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            kind = sniff(path)
            if kind == "gerber":
                gerbers[name] = GerberFile(path)
            elif kind == "drill":
                drills[name] = ExcellonFile(path)
            else:
                unparsed.append(name)
        except (GerberError, ValueError, OSError) as exc:
            raise GerberError(f"{name}: {exc}") from exc
    return gerbers, drills, unparsed

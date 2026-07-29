#!/usr/bin/env python3
"""Probe image and video assets and grade them against the team thresholds.

Stdlib only, so there is nothing to install beyond ffmpeg/ffprobe.

Why this exists as a script rather than instructions: eyeballing "is 1.9 MB over
the limit for a mobile banner" per-run produces different answers on different
days and from different people. The thresholds live in
references/thresholds.json and the arithmetic happens here, so every teammate
gets the same verdict from the same file.

Usage
  probe.py ASSET [ASSET ...] [--category CATEGORY] [--json]
                             [--no-overrides] [--show-config]

  ASSET           local path or http(s) URL
  --category      force an image category; otherwise inferred from the filename
  --json          machine-readable output instead of the markdown table
  --no-overrides  ignore user/project config and grade against team thresholds
  --show-config   print the active config layers and what they changed
  --progress      emit one line per asset to stderr as it completes
  --list-categories

Exit codes
  0  every asset compliant
  1  at least one asset non-compliant
  2  a probe failed, or a check could not be verified

  1 outranks 2: a definite non-compliance is more actionable than "could not
  verify", so it is never masked when both occur in one run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
THRESHOLDS_PATH = SKILL_DIR / "references" / "thresholds.json"

# Where brand- or project-specific overrides live. Deliberately outside the plugin:
# anything inside it is replaced wholesale by /plugin marketplace update.
CONFIG_BASENAMES = (".asset-check.json", "asset-check.config.json")
USER_CONFIG = Path.home() / ".claude" / "asset-check" / "config.json"
# Files that mark "this is the top of a project". The config search stops here so it
# cannot escape into a parent directory and quietly re-grade unrelated repositories.
PROJECT_MARKERS = (".git", ".hg", ".svn", "package.json", "pyproject.toml",
                   "go.mod", "Cargo.toml", "firebase.json")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".hevc"}
SVG_EXTS = {".svg"}

# ffprobe codec names -> the format vocabulary the thresholds use
CODEC_TO_FORMAT = {
    "mjpeg": "jpg",
    "jpeg": "jpg",
    "png": "png",
    "webp": "webp",
    "av1": "avif",
    "gif": "gif",
    "bmp": "bmp",
    "tiff": "tiff",
}

OK, WARN, FAIL = "pass", "warn", "fail"
# UNKNOWN is not a milder FAIL — it means the check could not be evaluated at all.
# Keeping it distinct matters because the alternative is asserting a fact that was
# never read: a remote video whose colour metadata did not arrive would otherwise be
# reported as "HDR: disabled — SDR — PASS", which is precisely backwards for the one
# check that exists to stop the app crashing.
UNKNOWN = "unknown"
MARK = {OK: "PASS", WARN: "WARN", FAIL: "FAIL", UNKNOWN: "UNKN"}

# ffprobe writes these to stderr while still exiting 0 and emitting usable JSON.
# When any appears, stream-level fields (pix_fmt, colour tags) may be absent simply
# because the data never arrived — not because the file lacks them.
INCOMPLETE_MARKERS = (
    "partial file",
    "error reading http response",
    "truncat",
    "invalid data found",
    "could not find codec parameters",
    "end of file",
)


# ---------------------------------------------------------------- helpers


def die(msg: str, code: int = 2) -> "None":
    print(f"asset-check: {msg}", file=sys.stderr)
    raise SystemExit(code)


def deep_merge(base: dict, over: dict, prefix: str = "",
               changed: "list | None" = None) -> dict:
    """Recursively merge `over` into a copy of `base`, recording what changed.

    Dicts merge; scalars and lists replace outright. Lists replace because a
    partially-merged list of formats or hints is never what anyone means.
    """
    result = dict(base)
    for key, value in over.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value, path + ".", changed)
        else:
            # Commentary is not a rule change. Counting it would inflate the override
            # count and make the provenance line untrustworthy.
            documentation = key.startswith("_") or key in ("notes", "$schema")
            if changed is not None and not documentation and result.get(key) != value:
                changed.append(path)
            result[key] = value
    return result


def find_project_config(start: "Path | None" = None) -> "Path | None":
    """Nearest config file walking up from cwd, bounded by the project.

    Walking up matters because assets usually live in a subfolder; someone running
    from `assets/banners/` should still pick up the config at their repo root.

    But the walk has to stop, and where it stops matters more than it sounds. An
    unbounded search reaches the filesystem root, so a single stray file in a parent
    directory silently re-grades every repository beneath it — and one in `$HOME`
    becomes a machine-wide config carrying *project* precedence, outranking the user
    layer that exists for exactly that purpose. So: stop at the project root, and
    never treat `$HOME` or above as a project.
    """
    here = (start or Path.cwd()).resolve()
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None

    for directory in (here, *here.parents):
        # Machine-wide preferences belong in USER_CONFIG, which is applied as the
        # lower-precedence user layer rather than as somebody's project.
        if home is not None and directory == home:
            break
        for name in CONFIG_BASENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        if any((directory / marker).exists() for marker in PROJECT_MARKERS):
            break
        if directory.parent == directory:  # filesystem root
            break
    return None


def normalise_category(key: str, rules: dict) -> dict:
    """Fill in the parts of a hand-written category that can be inferred.

    Someone adding a brand-specific category should not have to restate every field
    the grader happens to read, so anything derivable is derived. What cannot be
    guessed is required, and missing it is an error rather than a silent default —
    a category with an invented size limit would grade assets against a number
    nobody chose.
    """
    rules = dict(rules)
    for required in ("max_width_px", "max_bytes", "preferred_format"):
        if required not in rules:
            die(f"category '{key}' is missing required field '{required}'")
    if isinstance(rules["preferred_format"], str):
        rules["preferred_format"] = [rules["preferred_format"]]
    rules.setdefault("allowed_formats", list(rules["preferred_format"]))
    rules.setdefault("min_width_px", 0)
    rules.setdefault("preferred_width_px",
                     [rules["min_width_px"], rules["max_width_px"]])
    rules.setdefault("label", key.replace("-", " ").title())
    rules.setdefault("use_case", "custom")
    return rules


def load_thresholds(use_overrides: bool = True) -> tuple:
    """Return (thresholds, sources, changed).

    Layered lowest to highest: the bundled team canon, then the user's own config,
    then the project's. The bundled file stays authoritative for the team and is the
    one `verify-guidelines.py` checks against the guidelines doc; overrides are
    deliberately outside that check because they are *meant* to differ.

    Overrides live outside the plugin so `/plugin marketplace update` cannot wipe
    them — which is exactly what happened to the old self-editing approach.
    """
    if not THRESHOLDS_PATH.exists():
        die(f"thresholds.json not found at {THRESHOLDS_PATH}")
    thresholds = json.loads(THRESHOLDS_PATH.read_text())
    # Keep the team's categorisation inputs so a later override that silently
    # re-routes a category can be compared against it and reported.
    baseline = {
        "hint_priority": list(thresholds.get("hint_priority", [])),
        "filename_hints": dict(thresholds.get("filename_hints", {})),
    }
    sources = [("team", THRESHOLDS_PATH)]
    changed: list = []

    thresholds["_baseline"] = baseline

    if not use_overrides:
        return thresholds, sources, changed

    layers = []
    if USER_CONFIG.is_file():
        layers.append(("user", USER_CONFIG))
    env_config = os.environ.get("ASSET_CHECK_CONFIG")
    if env_config:
        env_path = Path(env_config).expanduser()
        if not env_path.is_file():
            die(f"ASSET_CHECK_CONFIG points at a missing file: {env_path}")
        layers.append(("env", env_path))
    else:
        project = find_project_config()
        if project:
            layers.append(("project", project))

    for label, path in layers:
        try:
            override = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            die(f"{path} is not valid JSON: {exc}")
        unknown = set(override) - set(thresholds) - {"$schema", "_comment", "notes"}
        if unknown:
            print(f"asset-check: warning: {path} has unrecognised top-level "
                  f"key(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        thresholds = deep_merge(thresholds, override, "", changed)
        sources.append((label, path))

    for key, rules in list(thresholds.get("image_categories", {}).items()):
        thresholds["image_categories"][key] = normalise_category(key, rules)

    # The global width cap is enforced ahead of any per-category limit, so raising a
    # category above it does nothing. Rejecting that outright beats half-applying it:
    # the alternative is a report that fails an asset against the global cap while
    # advising the larger category limit in the same breath.
    for global_field, fields in (
        ("hard_max_width_px", ("max_width_px", "hard_max_width_px")),
        ("hard_max_bytes", ("max_bytes",)),
    ):
        global_cap = thresholds["global"][global_field]
        for key, rules in thresholds.get("image_categories", {}).items():
            for field in fields:
                value = rules.get(field)
                if value and value > global_cap:
                    die(f"category '{key}' sets {field}={value}, above "
                        f"global.{global_field}={global_cap}, so it could never take "
                        f"effect.\n  Raise the global cap too:\n"
                        f'    {{"global": {{"{global_field}": {value}}}}}\n'
                        f"  or lower '{key}' to {global_cap} or less.")

    # A value touched by two layers is still one override; listing it twice would
    # misstate how much local config is actually in play.
    seen, unique = set(), []
    for path in changed:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return thresholds, sources, unique


def human_bytes(n: "int | None") -> str:
    if n is None:
        return "unknown"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def human_bitrate(bps: "int | None") -> str:
    return "unknown" if not bps else f"{bps / 1_000_000:.2f} Mbps"


def is_url(target: str) -> bool:
    return target.startswith(("http://", "https://"))


def ext_of(target: str) -> str:
    path = urllib.parse.urlparse(target).path if is_url(target) else target
    return os.path.splitext(path)[1].lower()


def remote_kind(url: str) -> str:
    """Fall back to the Content-Type header when a URL carries no extension.

    Signed and proxied CDN URLs frequently end in an opaque token rather than
    `.jpg`, and refusing those outright would make a headline feature ("just paste
    the CDN link") fail on exactly the links people paste.
    """
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method)
            if method == "GET":
                req.add_header("Range", "bytes=0-0")
            with urllib.request.urlopen(req, timeout=20) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        except (urllib.error.URLError, OSError):
            continue
        if ctype == "image/svg+xml":
            return "svg"
        if ctype.startswith("video/"):
            return "video"
        if ctype.startswith("image/"):
            return "image"
    return "unknown"


def kind_of(target: str) -> str:
    ext = ext_of(target)
    if ext in SVG_EXTS:
        return "svg"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if not ext and is_url(target):
        return remote_kind(target)
    return "unknown"


def infer_category(target: str, hints: dict, priority: list) -> str:
    """Guess an image category from the filename.

    Categories are tested in specificity order rather than by longest matching
    token. Token length is a misleading proxy: 'product-thumb.jpg' contains both
    'product' (7 chars) and 'thumb' (5), and picking the longer one grades a
    thumbnail against full product-image limits — demanding 1400 px of a 400 px
    file. Priority order settles these overlaps deliberately.
    """
    return resolve_category(target, hints, priority)[0]


def resolve_category(target: str, hints: dict, priority: list) -> tuple:
    """As `infer_category`, but also reports whether a hint actually matched.

    The distinction matters. `misc` has no filename hints at all, so landing there
    always means nothing matched — and `misc` carries the loosest limits in the
    config. Real CDN filenames rarely contain "product", "thumb" or "banner", so
    catalogue assets end up there routinely and an oversized grid thumbnail can pass
    silently. Callers need to be able to tell a considered category from a shrug.
    """
    name = os.path.basename(
        urllib.parse.urlparse(target).path if is_url(target) else target
    ).lower()
    ordered = priority + [c for c in hints if c not in priority]
    for category in ordered:
        if any(needle in name for needle in hints.get(category, ())):
            return category, True
    return "misc", False


def remote_size(url: str) -> "int | None":
    """Content-Length via HEAD, falling back to a ranged GET.

    Some CDNs (Shopify included) answer HEAD without Content-Length.
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as resp:
            length = resp.headers.get("Content-Length")
            if length:
                return int(length)
    except (urllib.error.URLError, ValueError, OSError):
        pass
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            crange = resp.headers.get("Content-Range")  # "bytes 0-0/12345"
            if crange and "/" in crange:
                total = crange.rsplit("/", 1)[1]
                if total.isdigit():
                    return int(total)
    except (urllib.error.URLError, ValueError, OSError):
        pass
    return None


def size_of(target: str) -> "int | None":
    if is_url(target):
        return remote_size(target)
    try:
        return os.path.getsize(target)
    except OSError:
        return None


# ---------------------------------------------------------------- probing


def exif_orientation(path: str) -> "int | None":
    """Read the EXIF Orientation tag from a local JPEG, or None.

    ffprobe reports stored dimensions and does not expose still-image orientation, so
    a portrait photo from a phone (stored landscape with Orientation=6) would be graded
    on the wrong axis — a 1600x900 file that actually displays as 900x1600.

    Parsed by hand because the point of this script is to need nothing beyond ffmpeg.
    Any malformed structure returns None; a bad guess here is worse than no guess.
    """
    try:
        with open(path, "rb") as fh:
            if fh.read(2) != b"\xff\xd8":  # not a JPEG
                return None
            while True:
                marker = fh.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                if marker[1] in (0xD9, 0xDA):  # end of metadata
                    return None
                size = int.from_bytes(fh.read(2), "big")
                if size < 2:
                    return None
                segment = fh.read(size - 2)
                if marker[1] != 0xE1 or not segment.startswith(b"Exif\x00\x00"):
                    continue

                tiff = segment[6:]
                if len(tiff) < 8:
                    return None
                byte_order = tiff[:2]
                if byte_order == b"MM":
                    endian = ">"
                elif byte_order == b"II":
                    endian = "<"
                else:
                    return None
                ifd_offset = struct.unpack(endian + "I", tiff[4:8])[0]
                if ifd_offset + 2 > len(tiff):
                    return None
                count = struct.unpack(endian + "H", tiff[ifd_offset:ifd_offset + 2])[0]
                for i in range(count):
                    entry = ifd_offset + 2 + i * 12
                    if entry + 12 > len(tiff):
                        return None
                    tag = struct.unpack(endian + "H", tiff[entry:entry + 2])[0]
                    if tag == 0x0112:  # Orientation
                        value = struct.unpack(endian + "H",
                                              tiff[entry + 8:entry + 10])[0]
                        return value if 1 <= value <= 8 else None
                return None
    except (OSError, struct.error, IndexError):
        return None


def run_ffprobe(target: str) -> dict:
    if not shutil.which("ffprobe"):
        die("ffprobe not found on PATH — install ffmpeg (see check-deps.sh)")
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        target,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"_error": "ffprobe timed out after 120s"}
    if out.returncode != 0:
        return {"_error": (out.stderr or "ffprobe failed").strip().splitlines()[-1]}
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {"_error": "ffprobe returned unparseable JSON"}

    # Keep the warnings even on success. ffprobe exits 0 while reporting things like
    # "stream 0, offset 0x30: partial file", and that line is the only evidence that
    # the metadata below is incomplete rather than genuinely absent.
    warnings = [ln.strip() for ln in (out.stderr or "").splitlines() if ln.strip()]
    data["_warnings"] = warnings
    blob = " ".join(warnings).lower()
    data["_incomplete"] = any(marker in blob for marker in INCOMPLETE_MARKERS)
    return data


def probe_svg(target: str) -> dict:
    """Parse SVG geometry directly.

    ffprobe reports 0x0 for SVG instead of failing, which would quietly grade
    every SVG as "0 px wide" and pass. Parsing the XML avoids that trap.
    """
    info = {"kind": "svg", "format": "svg", "bytes": size_of(target)}
    try:
        if is_url(target):
            with urllib.request.urlopen(target, timeout=20) as resp:
                raw = resp.read(65536).decode("utf-8", "replace")
                if info["bytes"] is None:
                    info["bytes"] = len(raw.encode())
        else:
            raw = Path(target).read_text(encoding="utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        info["_error"] = f"cannot read SVG: {exc}"
        return info

    def dim(attr: str) -> "float | None":
        """Absolute pixel width, or None when the value is relative.

        `width="100%"` is not 100 pixels and `width="3em"` is not 3 — treating the
        bare number as pixels made a 2400-unit graphic report as a compliant 100 px
        icon. Only unitless values and explicit `px` are pixels; anything else means
        the real geometry lives in the viewBox.
        """
        m = re.search(rf'\b{attr}\s*=\s*["\']\s*([\d.]+)\s*([a-z%]*)\s*["\']',
                      raw, re.I)
        if not m:
            m = re.search(rf'\b{attr}\s*=\s*([\d.]+)([a-z%]*)', raw, re.I)
        if not m:
            return None
        if m.group(2).lower() not in ("", "px"):
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None

    width, height = dim("width"), dim("height")

    vb = re.search(r'viewBox\s*=\s*["\']\s*([-\d.eE\s,]+)["\']', raw)
    if vb:
        parts = [p for p in re.split(r"[\s,]+", vb.group(1).strip()) if p]
        if len(parts) == 4:
            try:
                info["view_box"] = [float(p) for p in parts]
                width = width or info["view_box"][2]
                height = height or info["view_box"][3]
            except ValueError:
                pass

    info["width"] = int(width) if width else None
    info["height"] = int(height) if height else None

    # A scalable SVG needs a viewBox; without one it will not scale cleanly.
    # Geometry is read with regex rather than an XML parser on purpose: SVGs can
    # arrive from untrusted URLs, and stdlib XML parsers are open to entity-
    # expansion attacks. Nothing here needs a full parse tree.
    info["has_view_box"] = bool(vb)
    return info


def probe_raster(target: str) -> dict:
    data = run_ffprobe(target)
    if "_error" in data:
        return {"kind": "image", "_error": data["_error"], "bytes": size_of(target)}
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        return {"kind": "image", "_error": "no image stream found", "bytes": size_of(target)}
    st = streams[0]
    fmt = data.get("format", {})
    size = fmt.get("size")

    width, height = st.get("width"), st.get("height")
    # Orientations 5-8 rotate by 90 degrees, so the displayed dimensions are swapped
    # relative to what is stored. Grade what the user will actually see.
    orientation = None if is_url(target) else exif_orientation(target)
    if orientation in (5, 6, 7, 8) and width and height:
        width, height = height, width

    return {
        "kind": "image",
        "width": width,
        "height": height,
        "exif_orientation": orientation,
        "format": CODEC_TO_FORMAT.get(st.get("codec_name", ""), st.get("codec_name")),
        "pix_fmt": st.get("pix_fmt"),
        "bytes": int(size) if size and str(size).isdigit() else size_of(target),
    }


def parse_fps(rate: "str | None") -> "float | None":
    if not rate or "/" not in rate:
        return None
    num, den = rate.split("/", 1)
    try:
        num_f, den_f = float(num), float(den)
    except ValueError:
        return None
    return round(num_f / den_f, 3) if den_f else None


def probe_video(target: str) -> dict:
    data = run_ffprobe(target)
    if "_error" in data:
        return {"kind": "video", "_error": data["_error"], "bytes": size_of(target)}
    vs = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    if not vs:
        return {"kind": "video", "_error": "no video stream found", "bytes": size_of(target)}
    st, fmt = vs[0], data.get("format", {})

    bitrate = st.get("bit_rate") or fmt.get("bit_rate")
    try:
        bitrate = int(bitrate) if bitrate else None
    except (TypeError, ValueError):
        bitrate = None

    size = fmt.get("size")
    size = int(size) if size and str(size).isdigit() else size_of(target)

    # Derive bitrate when the container omits it — common for MOV.
    duration = fmt.get("duration")
    if bitrate is None and size and duration:
        try:
            dur = float(duration)
            if dur > 0:
                bitrate = int(size * 8 / dur)
        except (TypeError, ValueError):
            pass

    side = st.get("side_data_list") or []
    dovi = any(
        "dovi" in json.dumps(entry).lower() or "dolby" in json.dumps(entry).lower()
        for entry in side
    )

    # A fully-parsed video stream always reports pix_fmt. Its absence means the codec
    # parameters were never read, so every colour-related field below is unknown
    # rather than unset — treat the whole probe as incomplete.
    incomplete = bool(data.get("_incomplete")) or st.get("pix_fmt") is None

    return {
        "kind": "video",
        "incomplete": incomplete,
        "warnings": data.get("_warnings", []),
        "width": st.get("width"),
        "height": st.get("height"),
        "codec": st.get("codec_name"),
        "profile": st.get("profile"),
        "pix_fmt": st.get("pix_fmt"),
        "color_space": st.get("color_space"),
        "color_transfer": st.get("color_transfer"),
        "color_primaries": st.get("color_primaries"),
        "color_range": st.get("color_range"),
        "fps": parse_fps(st.get("r_frame_rate")),
        "bitrate_bps": bitrate,
        "duration_s": round(float(duration), 2) if duration else None,
        "container": fmt.get("format_name"),
        "extension": ext_of(target),
        "has_audio": any(
            s.get("codec_type") == "audio" for s in data.get("streams", [])
        ),
        "dolby_vision": dovi,
        "bytes": size,
    }


# ---------------------------------------------------------------- grading


def check(name: str, requirement: str, actual: str, status: str,
          remedy: str = "", fixable: bool = True) -> dict:
    return {
        "check": name,
        "requirement": requirement,
        "actual": actual,
        "status": status,
        "remedy": remedy,
        "fixable": fixable,
    }


def grade_image(info: dict, category: str, thresholds: dict) -> list:
    rules = thresholds["image_categories"][category]
    hard_w = thresholds["global"]["hard_max_width_px"]
    checks = []

    width = info.get("width")
    max_w = rules["max_width_px"]
    ceiling = rules.get("hard_max_width_px", max_w)
    min_w = rules.get("min_width_px", 0)

    if not width:
        checks.append(check("Width", f"{min_w}-{max_w} px", "unknown", FAIL,
                            "Could not read dimensions — verify the file is valid."))
    elif width > hard_w:
        checks.append(check("Width", f"<= {hard_w} px (global cap)", f"{width} px", FAIL,
                            f"Resize down to {max_w} px."))
    elif width > ceiling:
        checks.append(check("Width", f"<= {max_w} px", f"{width} px", FAIL,
                            f"Resize down to {max_w} px."))
    elif width > max_w:
        checks.append(check("Width", f"<= {max_w} px ({ceiling} px only if required)",
                            f"{width} px", WARN,
                            f"Acceptable only if the extra detail is needed; else resize to {max_w} px."))
    elif width < min_w:
        # Cannot be fixed by upscaling — that is exactly what the rules forbid.
        checks.append(check("Width", f">= {min_w} px (minimum)", f"{width} px", FAIL,
                            "Too small. Request a larger source — do NOT upscale.",
                            fixable=False))
    else:
        checks.append(check("Width", f"{min_w}-{max_w} px", f"{width} px", OK))

    size = info.get("bytes")
    max_b = rules["max_bytes"]
    pref_b = rules.get("preferred_bytes")
    # "Keep files under 1 MB" is a mandatory rule, so it needs a gate of its own
    # rather than relying on every category happening to set a lower limit. Mirrors
    # how the global width cap is applied.
    hard_b = thresholds["global"]["hard_max_bytes"]
    if size is None:
        checks.append(check("File size", f"<= {human_bytes(max_b)}", "unknown", WARN,
                            "Could not determine size (CDN withheld Content-Length)."))
    elif size > hard_b:
        checks.append(check("File size", f"<= {human_bytes(hard_b)} (global cap)",
                            human_bytes(size), FAIL,
                            "Resize first, then compress — do not crush quality to "
                            "hit the number."))
    elif size > max_b:
        checks.append(check("File size", f"<= {human_bytes(max_b)}", human_bytes(size), FAIL,
                            "Resize first, then compress — do not crush quality to hit the number."))
    elif pref_b and size > pref_b[1]:
        checks.append(check("File size", f"preferred <= {human_bytes(pref_b[1])}",
                            human_bytes(size), WARN, "Within limit but heavier than ideal."))
    else:
        checks.append(check("File size", f"<= {human_bytes(max_b)}", human_bytes(size), OK))

    fmt = (info.get("format") or "unknown").lower()
    preferred, allowed = rules["preferred_format"], rules["allowed_formats"]
    if rules.get("format_required") and fmt not in preferred:
        checks.append(check("Format", "/".join(preferred).upper(), fmt.upper(), FAIL,
                            "SVG is mandatory here. A raster source cannot be vectorised by this "
                            "tool — request a vector from the designer.",
                            fixable=False))
    elif fmt not in allowed:
        checks.append(check("Format", "/".join(allowed).upper(), fmt.upper(), FAIL,
                            f"Convert to {preferred[0].upper()}."))
    elif fmt not in preferred:
        remedy = ("PNG is only correct when transparency is required; otherwise convert to JPG."
                  if fmt == "png" else f"{preferred[0].upper()} is preferred.")
        checks.append(check("Format", "/".join(preferred).upper(), fmt.upper(), WARN, remedy))
    else:
        checks.append(check("Format", "/".join(preferred).upper(), fmt.upper(), OK))

    if info.get("kind") == "svg" and not info.get("has_view_box", True):
        checks.append(check("viewBox", "present", "missing", WARN,
                            "Without a viewBox the SVG will not scale cleanly."))
    return checks


def grade_video(info: dict, thresholds: dict) -> list:
    v = thresholds["video"]
    checks = []
    # When the read was incomplete, the colour and pixel-format checks below cannot be
    # answered. Reporting them as UNKNOWN keeps the tool honest; the remedy tells the
    # user how to get a real answer instead of leaving them with a fabricated pass.
    incomplete = bool(info.get("incomplete"))
    unreadable = ("could not be read from this source. Download the file and re-check "
                  "it locally for a real answer")

    w, h = info.get("width"), info.get("height")
    if not (w and h):
        checks.append(check("Resolution", f"<= {v['max_width_px']}x{v['max_height_px']}",
                            "unknown", FAIL, "Could not read dimensions."))
    else:
        # Orientation-agnostic: portrait 1080x1920 is as valid as 1920x1080.
        long_side, short_side = max(w, h), min(w, h)
        limit_long, limit_short = v["max_width_px"], v["max_height_px"]
        if long_side > limit_long or short_side > limit_short:
            checks.append(check("Resolution", f"<= {limit_long}x{limit_short}",
                                f"{w}x{h}", FAIL,
                                f"Scale down so the long edge is <= {limit_long} px."))
        else:
            status = OK if short_side <= v["preferred_height_px"] else WARN
            note = ("" if status == OK else
                    f"Within limits. {v['preferred_height_px']}p would be lighter on device "
                    "if this does not need the detail — no action required.")
            checks.append(check("Resolution", f"<= {limit_long}x{limit_short}",
                                f"{w}x{h}", status, note))

    codec = (info.get("codec") or "unknown").lower()
    checks.append(
        check("Codec", v["required_codec"].upper(), codec.upper(), OK)
        if codec == v["required_codec"]
        else check("Codec", v["required_codec"].upper(), codec.upper(), FAIL,
                   "Re-encode to H.264.")
    )

    br = info.get("bitrate_bps")
    if br is None:
        checks.append(check("Bitrate", f"<= {human_bitrate(v['max_bitrate_bps'])}",
                            "unknown", WARN, "Container did not report a bitrate."))
    elif br > v["max_bitrate_bps"]:
        checks.append(check("Bitrate", f"<= {human_bitrate(v['max_bitrate_bps'])}",
                            human_bitrate(br), FAIL,
                            f"Re-encode targeting {human_bitrate(v['target_bitrate_bps'])}."))
    else:
        checks.append(check("Bitrate", f"<= {human_bitrate(v['max_bitrate_bps'])}",
                            human_bitrate(br), OK))

    fps = info.get("fps")
    if fps is None:
        checks.append(check("FPS", f"<= {v['max_fps']}", "unknown", WARN))
    elif fps > v["max_fps"] + 0.5:
        checks.append(check("FPS", f"<= {v['max_fps']}", f"{fps:g}", FAIL,
                            f"Drop to {v['preferred_fps']} fps."))
    else:
        status = OK if fps <= v["preferred_fps"] + 0.5 else WARN
        note = "" if status == OK else f"{v['preferred_fps']} fps is preferred."
        checks.append(check("FPS", f"<= {v['max_fps']}", f"{fps:g}", status, note))

    pix = info.get("pix_fmt") or "unknown"
    if incomplete and not info.get("pix_fmt"):
        checks.append(check("Pixel format", v["required_pix_fmt"], "unreadable",
                            UNKNOWN, f"Pixel format {unreadable}."))
    elif pix == v["required_pix_fmt"]:
        checks.append(check("Pixel format", v["required_pix_fmt"], pix, OK))
    else:
        # yuvj420p means full-range luma, which washes out on mobile players.
        remedy = ("Full-range luma — convert with in_range=pc:out_range=tv (see image/video fixes)."
                  if pix.startswith("yuvj")
                  else f"Convert to {v['required_pix_fmt']}.")
        checks.append(check("Pixel format", v["required_pix_fmt"], pix, FAIL, remedy))

    hdr = v["hdr_markers"]
    cs = info.get("color_space") or ""
    ct = info.get("color_transfer") or ""
    is_hdr = cs in hdr["color_space"] or ct in hdr["color_transfer"] or info.get("dolby_vision")
    if is_hdr:
        # A positive HDR marker is trustworthy even from a partial read — the tag was
        # actually seen. Only the *absence* of markers is ambiguous.
        label = "Dolby Vision" if info.get("dolby_vision") else (ct or cs)
        checks.append(check("HDR", "disabled", f"HDR ({label})", FAIL,
                            "Scale + retag to bt709. Do NOT tone map — it desaturates badly."))
    elif incomplete and not cs and not ct:
        checks.append(check("HDR", "disabled", "unreadable", UNKNOWN,
                            f"Cannot confirm this is SDR: the colour tags {unreadable}. "
                            "Do not treat it as SDR-safe until verified.",
                            fixable=False))
    else:
        checks.append(check("HDR", "disabled", "SDR", OK))

    if incomplete and not cs:
        checks.append(check("Color space", v["required_color_space"], "unreadable",
                            UNKNOWN, f"Colour space {unreadable}.", fixable=False))
    elif not cs:
        # Untagged SDR H.264 is bt709 by convention; not worth a re-encode alone.
        checks.append(check("Color space", v["required_color_space"], "untagged", WARN,
                            "Treated as bt709. No action needed unless re-encoding anyway."))
    elif cs != v["required_color_space"]:
        checks.append(check("Color space", v["required_color_space"], cs, FAIL,
                            "Retag to bt709."))
    else:
        checks.append(check("Color space", v["required_color_space"], cs, OK))

    # Judge the container by extension, not by ffprobe's format_name. ffprobe
    # reports the whole demuxer family ("mov,mp4,m4a,3gp,3g2,mj2") for every
    # ISOBMFF file, so a real .mp4 and a .mov are indistinguishable there —
    # matching on it either warns about every asset or flags none of them. The
    # extension is also what actually ships to the player.
    family = (info.get("container") or "").lower()
    ext = (info.get("extension") or "").lstrip(".").lower()
    isobmff = "mp4" in family or "mov" in family
    if ext == "mp4":
        if isobmff:
            checks.append(check("Container", "MP4", "mp4", OK))
        else:
            checks.append(check("Container", "MP4", f"{ext} (actually {family})", FAIL,
                                "Extension and real container disagree — remux to MP4."))
    elif ext == "m4v":
        checks.append(check("Container", "MP4", "m4v", WARN,
                            "m4v is MP4 underneath; rename to .mp4 for consistency."))
    elif ext:
        checks.append(check("Container", "MP4", ext, FAIL, "Remux to MP4."))
    else:
        checks.append(check("Container", "MP4", family or "unknown", WARN,
                            "No file extension — confirm this is delivered as .mp4."))
    return checks


# ---------------------------------------------------------------- output


def make_progress(enabled: bool):
    """One line per asset as it finishes, or a no-op.

    Deliberately plain lines rather than a spinner. Inside a tool call neither
    stdout nor stderr is a TTY, so carriage-return animation is not rendered — it is
    captured verbatim, turning every frame into noise on a single line. Discrete
    lines stay readable when captured and stream as events when the run is
    backgrounded, which is when progress actually matters.

    Written to stderr so `--json` stdout stays machine-readable.
    """
    if not enabled:
        return lambda *_args, **_kw: None

    labels = {
        "compliant": "compliant",
        "compliant-with-warnings": "compliant (warnings)",
        "non-compliant": "NON-COMPLIANT",
        "unverified": "UNVERIFIED",
        "error": "probe failed",
    }

    def emit(index: int, total: int, target: str, verdict: str) -> None:
        print(f"[{index}/{total}] {os.path.basename(target) or target} — "
              f"{labels.get(verdict, verdict)}", file=sys.stderr, flush=True)

    return emit


def verdict_of(checks: list) -> str:
    if any(c["status"] == FAIL for c in checks):
        return "non-compliant"
    # Unverifiable ranks above warnings: an unanswered check is not a mild note, and
    # must never collapse into "compliant".
    if any(c["status"] == UNKNOWN for c in checks):
        return "unverified"
    if any(c["status"] == WARN for c in checks):
        return "compliant-with-warnings"
    return "compliant"


def render(results: list, sources: "list | None" = None,
           changed: "list | None" = None) -> str:
    lines = []
    # State the provenance up front when local rules are in play. Without it, a
    # teammate reading "3000 px PASS" has no way to tell that this project raised the
    # limit — they would reasonably conclude the team standard is looser than it is.
    if changed:
        origins = ", ".join(str(p) for label, p in (sources or []) if label != "team")
        lines.append(f"> Grading with {len(changed)} local override(s) from "
                     f"{origins}. Run with `--no-overrides` for the team baseline, "
                     f"or `--show-config` to see exactly what differs.\n")
    for r in results:
        lines.append(f"### {r['asset']}")
        if r.get("error"):
            lines.append(f"\n**Probe failed** — {r['error']}\n")
            continue
        meta = [f"kind: `{r['kind']}`"]
        if r["kind"] != "video":
            label = f"category: `{r['category']}`"
            if r.get("category_source") == "fallback":
                label += " (fallback — no filename hint matched)"
            meta.append(label)
        lines.append("\n" + " · ".join(meta) + "\n")
        # Say this out loud rather than letting misc read as a considered choice: it
        # has the loosest limits of any category, so an unmatched filename is the one
        # case where a pass deserves a second look.
        if r.get("category_source") == "fallback":
            lines.append(
                "> The filename matched no category, so this was graded against "
                "`misc`, which has the\n> loosest limits of any category. If it is "
                "really a product image, thumbnail, banner\n> or icon, re-run with "
                "`--category <name>` to grade it against the right thresholds.\n")
        lines.append("| Check | Requirement | Actual | Status |")
        lines.append("|---|---|---|---|")
        for c in r["checks"]:
            lines.append(
                f"| {c['check']} | {c['requirement']} | {c['actual']} | {MARK[c['status']]} |"
            )
        verdict = r["verdict"]
        if verdict == "compliant":
            lines.append("\n**Compliant**\n")
        else:
            problems = [c for c in r["checks"] if c["status"] in (FAIL, WARN, UNKNOWN)]
            label = {
                "non-compliant": "Non-compliant",
                "unverified": "Could not fully verify",
                "compliant-with-warnings": "Compliant, with warnings",
            }[verdict]
            lines.append(f"\n**{label}**")
            for c in problems:
                if c["remedy"]:
                    tag = "" if c["fixable"] else " _(not auto-fixable)_"
                    lines.append(f"- {c['check']}: {c['remedy']}{tag}")
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- main


def main() -> int:
    # Parsed in two passes: the override flags decide which categories exist, and
    # --category's choices depend on that.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--no-overrides", action="store_true")
    known, _ = pre.parse_known_args()

    thresholds, sources, changed = load_thresholds(use_overrides=not known.no_overrides)
    categories = sorted(thresholds["image_categories"])

    ap = argparse.ArgumentParser(
        prog="probe.py",
        description="Grade image/video assets against the team thresholds.",
    )
    ap.add_argument("assets", nargs="*", help="local paths and/or http(s) URLs")
    # Validated by hand rather than with argparse `choices`, so that a category which
    # exists only in local config can explain itself instead of looking like a typo.
    ap.add_argument("--category",
                    help="force an image category (default: inferred from filename)")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit JSON instead of the markdown table")
    ap.add_argument("--list-categories", action="store_true")
    ap.add_argument("--no-overrides", action="store_true",
                    help="ignore user and project config; grade against team "
                         "thresholds only (use this in CI)")
    ap.add_argument("--show-config", action="store_true",
                    help="print which config layers are active and what they changed")
    ap.add_argument("--progress", action="store_true",
                    help="emit one line per asset to stderr as it completes; useful "
                         "for long batches and when run in the background")
    args = ap.parse_args()

    if args.show_config:
        print("Active configuration, lowest precedence first:\n")
        for label, path in sources:
            print(f"  {label:8s} {path}")
        if changed:
            print(f"\n{len(changed)} value(s) overridden from the team defaults:")
            for path in changed:
                print(f"  - {path}")
        elif len(sources) > 1:
            print("\nNo values differ from the team defaults.")
        else:
            print("\nNo overrides found. Team defaults in effect.")
            print(f"\nTo add project rules, create one of {' or '.join(CONFIG_BASENAMES)}"
                  f"\nin your project root. For personal rules across all projects, use"
                  f"\n{USER_CONFIG}.")
        return 0

    if args.list_categories:
        for name in categories:
            rules = thresholds["image_categories"][name]
            origin = " (custom)" if any(
                c.startswith(f"image_categories.{name}") for c in changed) else ""
            print(f"{name:20s} {rules['label']} — {rules['use_case']}{origin}")
        return 0

    if args.category and args.category not in categories:
        # Distinguish "typo" from "suppressed": with --no-overrides the category may
        # genuinely exist in local config, and reporting only "invalid choice" sends
        # the user hunting for a spelling mistake that isn't there.
        if known.no_overrides:
            try:
                with_overrides, _, _ = load_thresholds(use_overrides=True)
            except SystemExit:
                with_overrides = {"image_categories": {}}
            if args.category in with_overrides.get("image_categories", {}):
                die(f"category '{args.category}' is defined in local config, but "
                    f"--no-overrides ignores local config.\n  Drop --no-overrides to "
                    f"use it, or choose one of: {', '.join(categories)}")
        die(f"unknown category '{args.category}'.\n  Available: "
            f"{', '.join(categories)}")

    if not args.assets:
        ap.error("no assets given")

    # Tracked as separate flags rather than one escalating number: a definite
    # non-compliance is more actionable than "could not verify", so it must not be
    # masked when both occur. A gate looking for "assets failed" would otherwise miss
    # a real failure that happened to share a run with an unreadable file.
    results = []
    any_fail = any_unverified = any_error = False
    progress = make_progress(args.progress)
    for target in args.assets:
        kind = kind_of(target)
        if kind == "unknown":
            results.append({"asset": target, "kind": "unknown",
                            "error": f"unrecognised extension '{ext_of(target) or 'none'}'"})
            any_error = True
            progress(len(results), len(args.assets), target, "error")
            continue

        if kind == "svg":
            info = probe_svg(target)
        elif kind == "video":
            info = probe_video(target)
        else:
            info = probe_raster(target)

        if info.get("_error"):
            results.append({"asset": target, "kind": kind, "error": info["_error"]})
            any_error = True
            progress(len(results), len(args.assets), target, "error")
            continue

        if kind == "video":
            if args.category:
                # Categories are an image concept; saying nothing would let the user
                # believe their override was applied to the video grading.
                print(f"note: --category {args.category} does not apply to video "
                      f"({target}); video limits are fixed", file=sys.stderr)
            checks, category = grade_video(info, thresholds), None
            category_source = None
        else:
            if args.category:
                category, category_source = args.category, "explicit"
            else:
                category, matched = resolve_category(
                    target, thresholds["filename_hints"],
                    thresholds.get("hint_priority", []))
                category_source = "hint" if matched else "fallback"
            # A reordered hint_priority can re-route a bundled category without
            # touching any limit — a 400px thumbnail graded as a product image then
            # fails as "too small, not auto-fixable", which looks like a real defect
            # in the asset. The tool knows both answers, so it should say so. Fires
            # only when an override actually changed the outcome, so default runs
            # stay silent.
            baseline = thresholds.get("_baseline") or {}
            if not args.category and baseline.get("hint_priority") != thresholds.get(
                    "hint_priority", []):
                baseline_category = infer_category(
                    target, baseline.get("filename_hints", {}),
                    baseline.get("hint_priority", []))
                if baseline_category != category:
                    print(f"note: hint_priority override graded "
                          f"{os.path.basename(target)} as '{category}'; the team "
                          f"baseline would use '{baseline_category}'. Pass "
                          f"--category to be explicit.", file=sys.stderr)
            # An SVG that landed in a JPG-preferring category is fine, not a defect.
            if info.get("format") == "svg" and category not in (
                "icon-ui", "icon-illustrative", "logo", "misc"
            ):
                # A hint did match here; it is being overridden for format reasons,
                # which is not the same as nothing having matched.
                category, category_source = "misc", "svg-adjusted"
            checks = grade_image(info, category, thresholds)

        verdict = verdict_of(checks)
        if verdict == "non-compliant":
            any_fail = True
        elif verdict == "unverified":
            # Same class as a failed probe: the tool could not answer, so a gate
            # should stop rather than infer approval from silence.
            any_unverified = True
        results.append({
            "asset": target, "kind": kind, "category": category,
            "category_source": category_source,
            "probe": info, "checks": checks, "verdict": verdict,
        })
        progress(len(results), len(args.assets), target, verdict)

    if args.as_json:
        print(json.dumps({
            "results": results,
            "config": {
                "sources": [{"layer": label, "path": str(path)} for label, path in sources],
                "overridden": changed,
            },
        }, indent=2))
    else:
        print(render(results, sources, changed))
    if any_fail:
        return 1
    return 2 if (any_unverified or any_error) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

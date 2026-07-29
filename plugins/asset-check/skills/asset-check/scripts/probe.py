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

  ASSET       local path or http(s) URL
  --category  force an image category; otherwise inferred from the filename
  --json      machine-readable output instead of the markdown table
  --list-categories

Exit codes
  0  every asset compliant
  1  at least one asset non-compliant
  2  a probe failed (missing file, ffprobe absent, unreadable asset)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
THRESHOLDS_PATH = SKILL_DIR / "references" / "thresholds.json"

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
MARK = {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}


# ---------------------------------------------------------------- helpers


def die(msg: str, code: int = 2) -> "None":
    print(f"asset-check: {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_thresholds() -> dict:
    if not THRESHOLDS_PATH.exists():
        die(f"thresholds.json not found at {THRESHOLDS_PATH}")
    with THRESHOLDS_PATH.open() as fh:
        return json.load(fh)


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
    path = urllib.request.urlparse(target).path if is_url(target) else target
    return os.path.splitext(path)[1].lower()


def kind_of(target: str) -> str:
    ext = ext_of(target)
    if ext in SVG_EXTS:
        return "svg"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return "unknown"


def infer_category(target: str, hints: dict, priority: list) -> str:
    """Guess an image category from the filename.

    Categories are tested in specificity order rather than by longest matching
    token. Token length is a misleading proxy: 'product-thumb.jpg' contains both
    'product' (7 chars) and 'thumb' (5), and picking the longer one grades a
    thumbnail against full product-image limits — demanding 1400 px of a 400 px
    file. Priority order settles these overlaps deliberately.
    """
    name = os.path.basename(
        urllib.request.urlparse(target).path if is_url(target) else target
    ).lower()
    ordered = priority + [c for c in hints if c not in priority]
    for category in ordered:
        if any(needle in name for needle in hints.get(category, ())):
            return category
    return "misc"


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
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {"_error": "ffprobe returned unparseable JSON"}


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
        m = re.search(rf'\b{attr}\s*=\s*["\']?\s*([\d.]+)', raw)
        return float(m.group(1)) if m else None

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
    return {
        "kind": "image",
        "width": st.get("width"),
        "height": st.get("height"),
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

    return {
        "kind": "video",
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
    if size is None:
        checks.append(check("File size", f"<= {human_bytes(max_b)}", "unknown", WARN,
                            "Could not determine size (CDN withheld Content-Length)."))
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
    if pix == v["required_pix_fmt"]:
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
        label = "Dolby Vision" if info.get("dolby_vision") else (ct or cs)
        checks.append(check("HDR", "disabled", f"HDR ({label})", FAIL,
                            "Scale + retag to bt709. Do NOT tone map — it desaturates badly."))
    else:
        checks.append(check("HDR", "disabled", "SDR", OK))

    if not cs:
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


def verdict_of(checks: list) -> str:
    if any(c["status"] == FAIL for c in checks):
        return "non-compliant"
    if any(c["status"] == WARN for c in checks):
        return "compliant-with-warnings"
    return "compliant"


def render(results: list) -> str:
    lines = []
    for r in results:
        lines.append(f"### {r['asset']}")
        if r.get("error"):
            lines.append(f"\n**Probe failed** — {r['error']}\n")
            continue
        meta = [f"kind: `{r['kind']}`"]
        if r["kind"] != "video":
            meta.append(f"category: `{r['category']}`")
        lines.append("\n" + " · ".join(meta) + "\n")
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
            problems = [c for c in r["checks"] if c["status"] in (FAIL, WARN)]
            label = ("Non-compliant" if verdict == "non-compliant"
                     else "Compliant, with warnings")
            lines.append(f"\n**{label}**")
            for c in problems:
                if c["remedy"]:
                    tag = "" if c["fixable"] else " _(not auto-fixable)_"
                    lines.append(f"- {c['check']}: {c['remedy']}{tag}")
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- main


def main() -> int:
    thresholds = load_thresholds()
    categories = sorted(thresholds["image_categories"])

    ap = argparse.ArgumentParser(
        prog="probe.py",
        description="Grade image/video assets against the team thresholds.",
    )
    ap.add_argument("assets", nargs="*", help="local paths and/or http(s) URLs")
    ap.add_argument("--category", choices=categories,
                    help="force an image category (default: inferred from filename)")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit JSON instead of the markdown table")
    ap.add_argument("--list-categories", action="store_true")
    args = ap.parse_args()

    if args.list_categories:
        for name in categories:
            rules = thresholds["image_categories"][name]
            print(f"{name:20s} {rules['label']} — {rules['use_case']}")
        return 0

    if not args.assets:
        ap.error("no assets given")

    results, worst = [], 0
    for target in args.assets:
        kind = kind_of(target)
        if kind == "unknown":
            results.append({"asset": target, "kind": "unknown",
                            "error": f"unrecognised extension '{ext_of(target) or 'none'}'"})
            worst = max(worst, 2)
            continue

        if kind == "svg":
            info = probe_svg(target)
        elif kind == "video":
            info = probe_video(target)
        else:
            info = probe_raster(target)

        if info.get("_error"):
            results.append({"asset": target, "kind": kind, "error": info["_error"]})
            worst = max(worst, 2)
            continue

        if kind == "video":
            checks, category = grade_video(info, thresholds), None
        else:
            category = args.category or infer_category(
                target, thresholds["filename_hints"], thresholds.get("hint_priority", [])
            )
            # An SVG that landed in a JPG-preferring category is fine, not a defect.
            if info.get("format") == "svg" and category not in (
                "icon-ui", "icon-illustrative", "logo", "misc"
            ):
                category = "misc"
            checks = grade_image(info, category, thresholds)

        verdict = verdict_of(checks)
        if verdict == "non-compliant":
            worst = max(worst, 1)
        results.append({
            "asset": target, "kind": kind, "category": category,
            "probe": info, "checks": checks, "verdict": verdict,
        })

    if args.as_json:
        print(json.dumps({"results": results}, indent=2))
    else:
        print(render(results))
    return worst


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

#!/usr/bin/env python3
"""Assert that thresholds.json still matches references/asset-guidelines.md.

Two files hold the same numbers: the markdown doc people read, and the JSON the
tooling enforces. Asking contributors to remember to update both is exactly how the
two drift apart, and a drifted pair is worse than either alone — the team follows a
document the tooling contradicts. So the agreement is checked mechanically instead.

Run this after editing either file. Wire it into CI or a pre-commit hook if you want
the guarantee enforced rather than requested.

Usage
  verify-guidelines.py [--quiet]

Exit codes
  0  the two agree
  1  they disagree (differences are printed)
  2  a file was missing or unparseable
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
THRESHOLDS = SKILL_DIR / "references" / "thresholds.json"
# Both files sit inside the skill on purpose. Anything outside plugins/asset-check/ is
# not packaged when a teammate installs the plugin, so a guidelines doc at the repo
# root would be missing for every installed user — and this script would resolve it to
# a path that does not exist.
GUIDELINES = SKILL_DIR / "references" / "asset-guidelines.md"

KB = 1024
DASH = r"[–—-]"  # the doc uses an en dash; accept hyphen and em dash too


def die(msg: str) -> "None":
    print(f"verify-guidelines: {msg}", file=sys.stderr)
    raise SystemExit(2)


def to_bytes(value: float, unit: str) -> int:
    return int(value * (KB * KB if unit.upper() == "MB" else KB))


def parse_preferred(cell: str) -> tuple:
    """'300–500 px, 50–150 KB' -> ((300, 500), (51200, 153600))

    Also handles '1400–1600 px' (no size), '24–64 px, <20 KB' and
    '≤1600 px, ≤500 KB'.
    """
    width = re.search(rf"(\d+)\s*{DASH}\s*(\d+)\s*px", cell)
    if width:
        wrange = (int(width.group(1)), int(width.group(2)))
    else:
        capped = re.search(r"[≤<]\s*(\d+)\s*px", cell)
        if not capped:
            raise ValueError(f"no width in preferred cell: {cell!r}")
        wrange = (0, int(capped.group(1)))

    size = re.search(rf"(\d+)\s*{DASH}\s*(\d+)\s*(KB|MB)", cell)
    if size:
        brange = (to_bytes(int(size.group(1)), size.group(3)),
                  to_bytes(int(size.group(2)), size.group(3)))
    else:
        capped = re.search(r"[≤<]\s*(\d+)\s*(KB|MB)", cell)
        brange = (0, to_bytes(int(capped.group(1)), capped.group(2))) if capped else None
    return wrange, brange


def parse_max(cell: str) -> tuple:
    """'2000 px (max 2400 px if required)' -> (2000, 2400, None)
    '800 px, 300 KB' -> (800, None, 307200)
    """
    widths = re.findall(r"(\d+)\s*px", cell)
    if not widths:
        raise ValueError(f"no width in max cell: {cell!r}")
    max_w = int(widths[0])
    hard_w = int(widths[1]) if len(widths) > 1 else None
    size = re.search(r"(\d+)\s*(KB|MB)", cell)
    max_b = to_bytes(int(size.group(1)), size.group(2)) if size else None
    return max_w, hard_w, max_b


def markdown_tables(text: str) -> list:
    """Return every pipe table as a list of row-cell-lists (header excluded)."""
    tables, current = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue  # separator row
            current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def main() -> int:
    ap = argparse.ArgumentParser(prog="verify-guidelines.py")
    ap.add_argument("--quiet", action="store_true", help="only report problems")
    args = ap.parse_args()

    for path in (THRESHOLDS, GUIDELINES):
        if not path.exists():
            die(f"not found: {path}")

    thresholds = json.loads(THRESHOLDS.read_text())
    doc = GUIDELINES.read_text()
    tables = markdown_tables(doc)

    image_rows, video_rows = [], []
    for table in tables:
        header = [c.lower() for c in table[0]]
        if "key" in header and "preferred size" in header:
            image_rows = table[1:]
        elif header[:2] == ["setting", "value"]:
            video_rows = table[1:]
    if not image_rows:
        die("could not find the image table (needs 'Key' and 'Preferred Size' columns)")
    if not video_rows:
        die("could not find the video table (needs 'Setting' and 'Value' columns)")

    problems = []
    cats = thresholds["image_categories"]
    seen = set()

    for row in image_rows:
        label, key, _use, pref, mx, fmt = row[0], row[1].strip("`"), row[2], row[3], row[4], row[5]
        seen.add(key)
        if key not in cats:
            problems.append(f"{key}: in the doc but missing from thresholds.json")
            continue
        r = cats[key]
        try:
            (pw_lo, pw_hi), pref_bytes = parse_preferred(pref)
            max_w, hard_w, max_b = parse_max(mx)
        except (ValueError, AttributeError) as exc:
            problems.append(f"{key}: cannot parse the doc row — {exc}")
            continue

        def cmp(field, expected, actual):
            if expected != actual:
                problems.append(f"{key}.{field}: doc says {expected}, thresholds.json has {actual}")

        cmp("min_width_px", pw_lo, r.get("min_width_px"))
        cmp("preferred_width_px", [pw_lo, pw_hi], list(r.get("preferred_width_px", [])))
        cmp("max_width_px", max_w, r.get("max_width_px"))
        cmp("hard_max_width_px", hard_w, r.get("hard_max_width_px"))
        if pref_bytes is not None:
            cmp("preferred_bytes", list(pref_bytes),
                list(r["preferred_bytes"]) if r.get("preferred_bytes") else None)
        # A row with no explicit max size inherits the global 1 MB cap.
        cmp("max_bytes", max_b if max_b is not None else thresholds["global"]["hard_max_bytes"],
            r.get("max_bytes"))
        cmp("preferred_format", [fmt.lower()], [f.lower() for f in r.get("preferred_format", [])])
        if label.lower().startswith(("icon", "logo")) and not r.get("format_required"):
            problems.append(f"{key}: doc mandates {fmt} but format_required is not set")

    for orphan in sorted(set(cats) - seen):
        problems.append(f"{orphan}: in thresholds.json but not documented in asset-guidelines.md")

    # ---- video -----------------------------------------------------------
    v = thresholds["video"]
    vals = {r[0].strip().lower(): r[1].strip() for r in video_rows}

    def need(setting: str) -> str:
        if setting not in vals:
            problems.append(f"video.{setting}: row missing from the doc")
            return ""
        return vals[setting]

    res = need("resolution")
    if res:
        nums = [int(n) for n in re.findall(r"(\d+)p", res)]
        if nums:
            if min(nums) != v["preferred_height_px"]:
                problems.append(f"video.preferred_height_px: doc says {min(nums)}, "
                                f"thresholds.json has {v['preferred_height_px']}")
            if max(nums) != v["max_height_px"]:
                problems.append(f"video.max_height_px: doc says {max(nums)}, "
                                f"thresholds.json has {v['max_height_px']}")

    br = need("bitrate")
    if br:
        nums = [float(n) for n in re.findall(r"([\d.]+)\s*(?:Mbps)?", br) if n]
        if nums:
            hi = max(nums)
            if int(hi * 1_000_000) != v["max_bitrate_bps"]:
                problems.append(f"video.max_bitrate_bps: doc caps at {hi} Mbps, "
                                f"thresholds.json has {v['max_bitrate_bps']}")
            lo = min(nums)
            if not (lo * 1_000_000 <= v["target_bitrate_bps"] <= hi * 1_000_000):
                problems.append(f"video.target_bitrate_bps {v['target_bitrate_bps']} "
                                f"outside the doc's {lo}–{hi} Mbps range")

    fps = need("fps")
    if fps:
        nums = [int(n) for n in re.findall(r"(\d+)", fps)]
        if nums:
            if min(nums) != v["preferred_fps"]:
                problems.append(f"video.preferred_fps: doc says {min(nums)}, "
                                f"thresholds.json has {v['preferred_fps']}")
            if max(nums) != v["max_fps"]:
                problems.append(f"video.max_fps: doc says {max(nums)}, "
                                f"thresholds.json has {v['max_fps']}")

    for setting, field, transform in (
        ("codec", "required_codec", lambda s: s.lower().replace(".", "")),
        ("pixel format", "required_pix_fmt", lambda s: s.lower()),
        ("color space", "required_color_space", lambda s: s.lower()),
        ("container", "required_container", lambda s: s.lower()),
    ):
        raw = need(setting)
        if raw and transform(raw) != v[field]:
            problems.append(f"video.{field}: doc says {transform(raw)!r}, "
                            f"thresholds.json has {v[field]!r}")

    hdr = need("hdr")
    if hdr and (hdr.lower() == "disabled") != (v["hdr_allowed"] is False):
        problems.append(f"video.hdr_allowed contradicts the doc ({hdr!r})")

    # ---- mandatory rules --------------------------------------------------
    rules_block = re.search(r"##\s*Mandatory Rules\s*(.+?)(?=\n##\s)", doc, re.S)
    if not rules_block:
        problems.append("could not find the Mandatory Rules section")
    else:
        doc_rules = len(re.findall(r"^\s*-\s+", rules_block.group(1), re.M))
        json_rules = len(thresholds["global"]["rules"])
        if doc_rules != json_rules:
            problems.append(f"mandatory rules: doc lists {doc_rules}, "
                            f"thresholds.json has {json_rules}")
        cap = re.search(r"wider than (\d+)\s*px", rules_block.group(1))
        if cap and int(cap.group(1)) != thresholds["global"]["hard_max_width_px"]:
            problems.append(f"global.hard_max_width_px: doc says {cap.group(1)}, "
                            f"thresholds.json has {thresholds['global']['hard_max_width_px']}")

    if problems:
        print(f"thresholds.json disagrees with {GUIDELINES.name} "
              f"({len(problems)} issue{'s' if len(problems) != 1 else ''}):\n")
        for p in problems:
            print(f"  - {p}")
        print("\nThe doc is the authority — update thresholds.json to match it.")
        return 1

    if not args.quiet:
        print(f"thresholds.json matches {GUIDELINES.name} "
              f"({len(image_rows)} image categories, {len(video_rows)} video settings, "
              f"{len(thresholds['global']['rules'])} mandatory rules).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Regression tests for the asset-check plugin.

Each test here corresponds to a defect found by systematic debugging. They exist so
the same bug cannot come back quietly — several of these failure modes produced
output that looked perfectly reasonable, which is exactly why they survived review.

Run:  python3 tests/test_asset_check.py
Needs ffmpeg on PATH (already a hard requirement of the plugin). No pip installs.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "plugins" / "asset-check" / "skills" / "asset-check"
SCRIPTS = SKILL / "scripts"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = load("probe", SCRIPTS / "probe.py")
THRESHOLDS = json.loads((SKILL / "references" / "thresholds.json").read_text())


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", *args, "-y"], check=True,
                   capture_output=True)


# --------------------------------------------------------------------------
# High #1 — SKILL.md must not tell the model to run relative script paths.
# The skill's cwd is the user's project, so "python3 scripts/probe.py" resolves
# to <user-project>/scripts/probe.py and fails.
# --------------------------------------------------------------------------
class TestSkillInvocationPaths(unittest.TestCase):
    def test_skill_md_has_no_bare_relative_script_invocation(self):
        text = (SKILL / "SKILL.md").read_text()
        offenders = []
        for m in re.finditer(r"^\s*(?:python3|bash)\s+(scripts/\S+)", text, re.M):
            offenders.append(m.group(0).strip())
        self.assertEqual(
            offenders, [],
            "SKILL.md invokes scripts by a path relative to the skill dir, but the "
            "skill runs from the user's project directory:\n  " + "\n  ".join(offenders),
        )

    def test_skill_md_explains_how_to_locate_scripts(self):
        text = (SKILL / "SKILL.md").read_text()
        self.assertIn(
            "CLAUDE_PLUGIN_ROOT", text,
            "SKILL.md should reference ${CLAUDE_PLUGIN_ROOT} (or the injected skill "
            "base directory) so script paths resolve regardless of cwd",
        )


# --------------------------------------------------------------------------
# High #2 — the guidelines doc must ship inside the plugin. Anything outside
# plugins/asset-check/ is not packaged when a teammate installs it.
# --------------------------------------------------------------------------
class TestGuidelinesArePackaged(unittest.TestCase):
    PLUGIN = REPO / "plugins" / "asset-check"

    def test_guidelines_doc_lives_inside_the_plugin(self):
        hits = list(self.PLUGIN.rglob("asset-guidelines.md"))
        self.assertTrue(
            hits,
            "asset-guidelines.md is not inside plugins/asset-check/, so installed "
            "users will not have it and SKILL.md's pointer will dangle",
        )

    def test_verifier_works_from_a_plugin_only_install(self):
        """Copy just the plugin dir, as installing does, and run the verifier."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "asset-check"
            shutil.copytree(self.PLUGIN, dest)
            out = subprocess.run(
                [sys.executable, str(dest / "skills" / "asset-check" / "scripts"
                                     / "verify-guidelines.py")],
                capture_output=True, text=True,
            )
            self.assertEqual(
                out.returncode, 0,
                f"verify-guidelines.py fails in a plugin-only tree "
                f"(exit {out.returncode}):\n{out.stdout}{out.stderr}",
            )


# --------------------------------------------------------------------------
# High #3 / #4 — unreadable metadata must not be graded as confirmed-SDR.
# Reproduced against a real CDN: remote video can come back with pix_fmt and
# color_space absent, and the HDR check then reported "disabled / SDR / PASS".
# --------------------------------------------------------------------------
class TestUnreadableMetadataIsNotAPass(unittest.TestCase):
    def _grade(self, **over):
        info = {
            "kind": "video", "width": 3840, "height": 2160, "codec": "h264",
            "pix_fmt": None, "color_space": None, "color_transfer": None,
            "color_range": None, "fps": 30.0, "bitrate_bps": 4_000_000,
            "container": "mov,mp4,m4a,3gp,3g2,mj2", "extension": ".mp4",
            "bytes": 1000, "dolby_vision": False, "incomplete": True,
        }
        info.update(over)
        return {c["check"]: c for c in probe.grade_video(info, THRESHOLDS)}

    def test_hdr_is_not_reported_as_sdr_when_metadata_unreadable(self):
        hdr = self._grade()["HDR"]
        self.assertNotEqual(
            hdr["status"], probe.OK,
            "HDR check passed on a video whose colour metadata could not be read — "
            "this is a silent false negative on the most safety-critical check",
        )
        self.assertNotIn(
            "SDR", hdr["actual"],
            "reporting 'SDR' asserts a fact that was never read",
        )

    def test_colour_space_not_treated_as_benign_when_unreadable(self):
        cs = self._grade()["Color space"]
        self.assertNotEqual(cs["status"], probe.OK)
        self.assertNotIn(
            "No action needed", cs["remedy"],
            "an unreadable colour space must not be described as needing no action",
        )

    def test_complete_probe_with_genuinely_absent_tags_still_only_warns(self):
        """A fully-read SDR file that simply carries no colour tags is bt709 by
        convention. That must stay a WARN, not become a hard failure."""
        checks = self._grade(incomplete=False, pix_fmt="yuv420p")
        self.assertEqual(checks["HDR"]["status"], probe.OK)
        self.assertEqual(checks["Color space"]["status"], probe.WARN)

    def test_real_hdr_file_still_fails(self):
        checks = self._grade(incomplete=False, pix_fmt="yuv420p",
                             color_space="bt2020nc")
        self.assertEqual(checks["HDR"]["status"], probe.FAIL)

    def test_probe_records_ffprobe_warnings(self):
        """ffprobe printed 'partial file' and probe.py threw it away."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "v.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:d=1:r=10",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src))
            info = probe.probe_video(str(src))
        self.assertIn("incomplete", info,
                      "probe_video should report whether the read was complete")
        self.assertFalse(info["incomplete"], "a local complete file is not incomplete")


# --------------------------------------------------------------------------
# Medium #6 — EXIF orientation. A portrait phone photo is stored landscape with
# Orientation=6; grading the stored dimensions checks the wrong axis.
# --------------------------------------------------------------------------
class TestExifOrientation(unittest.TestCase):
    @staticmethod
    def _with_orientation(path: Path, orientation: int) -> Path:
        raw = path.read_bytes()
        exif = (b"Exif\x00\x00" + b"MM\x00*\x00\x00\x00\x08"
                + b"\x00\x01" + b"\x01\x12\x00\x03\x00\x00\x00\x01"
                + struct.pack(">H", orientation) + b"\x00\x00"
                + b"\x00\x00\x00\x00")
        app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
        out = path.with_name("rotated.jpg")
        out.write_bytes(raw[:2] + app1 + raw[2:])
        return out

    def test_orientation_6_swaps_reported_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "flat.jpg"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=1600x900:d=1:r=1",
                   "-frames:v", "1", str(src))
            rot = self._with_orientation(src, 6)
            info = probe.probe_raster(str(rot))
        self.assertEqual(
            (info["width"], info["height"]), (900, 1600),
            "EXIF Orientation=6 means the image displays as portrait; grading "
            f"{info['width']}x{info['height']} checks the wrong axis",
        )

    def test_orientation_1_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "flat.jpg"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=1600x900:d=1:r=1",
                   "-frames:v", "1", str(src))
            rot = self._with_orientation(src, 1)
            info = probe.probe_raster(str(rot))
        self.assertEqual((info["width"], info["height"]), (1600, 900))


# --------------------------------------------------------------------------
# Medium #7 — SVG units. width="100%" was read as 100px.
# --------------------------------------------------------------------------
class TestSvgUnits(unittest.TestCase):
    def _probe(self, svg: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "i.svg"
            p.write_text(svg)
            return probe.probe_svg(str(p))

    def test_percentage_width_falls_back_to_viewbox(self):
        info = self._probe('<svg xmlns="http://www.w3.org/2000/svg" width="100%" '
                           'height="100%" viewBox="0 0 2400 1200"><rect/></svg>')
        self.assertEqual(
            info["width"], 2400,
            f"width='100%' was read as {info['width']}px; percentages are not pixels "
            "and the viewBox is the real geometry",
        )

    def test_em_width_falls_back_to_viewbox(self):
        info = self._probe('<svg xmlns="http://www.w3.org/2000/svg" width="3em" '
                           'viewBox="0 0 48 48"><rect/></svg>')
        self.assertEqual(info["width"], 48)

    def test_explicit_px_is_honoured(self):
        info = self._probe('<svg xmlns="http://www.w3.org/2000/svg" width="64px" '
                           'height="64px" viewBox="0 0 32 32"><rect/></svg>')
        self.assertEqual(info["width"], 64, "an explicit px width wins over viewBox")

    def test_bare_number_is_pixels(self):
        info = self._probe('<svg xmlns="http://www.w3.org/2000/svg" width="48" '
                           'height="48"><rect/></svg>')
        self.assertEqual(info["width"], 48)

    def test_percentage_with_no_viewbox_is_unknown_not_a_pass(self):
        info = self._probe('<svg xmlns="http://www.w3.org/2000/svg" '
                           'width="100%"><rect/></svg>')
        self.assertIsNone(info["width"],
                          "with no viewBox and no absolute width, the size is unknown")


# --------------------------------------------------------------------------
# Medium #5 — the documented ffmpeg fix must not hard-fail on silent video.
# --------------------------------------------------------------------------
class TestDocumentedCommands(unittest.TestCase):
    def test_audio_maps_are_optional(self):
        text = (SKILL / "references" / "video-fixes.md").read_text()
        bad = re.findall(r"-map\s+0:a:0(?!\?)", text)
        self.assertEqual(
            bad, [],
            "video-fixes.md uses '-map 0:a:0' without a trailing '?'. On a silent "
            "video ffmpeg aborts with \"Stream map '' matches no streams\".",
        )

    def test_the_documented_command_actually_runs_on_silent_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "silent.mp4", Path(tmp) / "out.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:d=1:r=30",
                   "-c:v", "libx264", str(src))
            out = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(src),
                 "-map", "0:v:0", "-map", "0:a:0?",
                 "-vf", "scale='min(1920,iw)':-2:flags=lanczos,format=yuv420p",
                 "-c:v", "libx264", "-c:a", "aac", str(dst), "-y"],
                capture_output=True, text=True,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(dst.exists())


# --------------------------------------------------------------------------
# Low #8 — urlparse came from an undocumented re-export of urllib.request.
# --------------------------------------------------------------------------
class TestImports(unittest.TestCase):
    def test_urlparse_is_imported_from_urllib_parse(self):
        src = (SCRIPTS / "probe.py").read_text()
        self.assertNotIn(
            "urllib.request.urlparse", src,
            "urlparse belongs to urllib.parse; reaching it through urllib.request "
            "relies on an internal re-export that is not part of the public API",
        )
        self.assertRegex(src, r"import urllib\.parse|from urllib\.parse import")


# --------------------------------------------------------------------------
# Low #9 — --category is meaningless for video and was silently ignored.
# Low #11 — extensionless URLs: the original command promised Content-Type
# sniffing, which the rewrite dropped.
# --------------------------------------------------------------------------
class TestUsabilityRegressions(unittest.TestCase):
    def test_category_with_video_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "v.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:d=1:r=10",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src))
            out = subprocess.run(
                [sys.executable, str(SCRIPTS / "probe.py"),
                 "--category", "logo", str(src)],
                capture_output=True, text=True,
            )
        self.assertIn(
            "categor", (out.stdout + out.stderr).lower(),
            "--category silently did nothing for a video; say so rather than "
            "letting the user believe it applied",
        )

    def test_extensionless_url_uses_content_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            img = Path(tmp) / "payload"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=400x400:d=1:r=1",
                   "-frames:v", "1", "-f", "mjpeg", str(img))

            class H(SimpleHTTPRequestHandler):
                def do_HEAD(self): self._send()
                def do_GET(self):
                    self._send()
                    self.wfile.write(img.read_bytes())
                def _send(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(img.stat().st_size))
                    self.end_headers()
                def log_message(self, *a): pass

            srv = HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{srv.server_address[1]}/payload"
            try:
                out = subprocess.run(
                    [sys.executable, str(SCRIPTS / "probe.py"), url],
                    capture_output=True, text=True,
                )
            finally:
                srv.shutdown()
                srv.server_close()
        self.assertNotIn(
            "unrecognised extension", out.stdout,
            "an extensionless URL should fall back to the Content-Type header",
        )


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required to run these tests")
    unittest.main(verbosity=2)

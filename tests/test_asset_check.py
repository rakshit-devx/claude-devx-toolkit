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


# Importing probe.py would otherwise drop a __pycache__ inside the plugin. That is
# more than untidy: a local-path `claude plugin install` copies the working tree
# rather than the git archive, so .gitignore does not apply and the bytecode ships to
# whoever installs it. Keep the plugin directory clean of build artifacts entirely.
sys.dont_write_bytecode = True


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


_EMPTY_HOME: "str | None" = None


def empty_home() -> str:
    """A HOME with no asset-check config, shared across the run."""
    global _EMPTY_HOME
    if _EMPTY_HOME is None:
        _EMPTY_HOME = tempfile.mkdtemp(prefix="asset-check-empty-home-")
    return _EMPTY_HOME


def probe_env(home: "Path | str | None" = None) -> dict:
    """Environment for probe.py subprocesses.

    HOME points somewhere empty so a developer's real
    ~/.claude/asset-check/config.json cannot change results, and ASSET_CHECK_CONFIG is
    cleared for the same reason.

    Crucially HOME is *not* the project directory: config discovery stops before
    reaching $HOME, so a HOME equal to cwd would suppress the project config the test
    is trying to exercise. Pass an explicit home only when testing that boundary.
    """
    env = dict(os.environ)
    env["HOME"] = str(home) if home is not None else empty_home()
    env.pop("ASSET_CHECK_CONFIG", None)
    return env


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

    def test_no_build_artifacts_in_the_plugin_tree(self):
        """A local-path install copies the working tree, so .gitignore does not
        protect anyone — whatever is on disk ships. A stale __pycache__ reached a
        real install this way."""
        junk = [p for p in self.PLUGIN.rglob("*")
                if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo")
                or p.name == ".DS_Store"]
        self.assertEqual(
            [str(p.relative_to(REPO)) for p in junk], [],
            "build artifacts in the plugin directory would be packaged on install",
        )

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
                capture_output=True, text=True, env=probe_env(),
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
                capture_output=True, text=True, env=probe_env(),
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
                    capture_output=True, text=True, env=probe_env(),
                )
            finally:
                srv.shutdown()
                srv.server_close()
        self.assertNotIn(
            "unrecognised extension", out.stdout,
            "an extensionless URL should fall back to the Content-Type header",
        )


# --------------------------------------------------------------------------
# Brand / project overrides. These must survive `/plugin marketplace update`,
# which is why they live outside the plugin rather than editing it in place.
# --------------------------------------------------------------------------
class TestBrandOverrides(unittest.TestCase):
    PROBE = SCRIPTS / "probe.py"

    def _run(self, *args, cwd=None, env=None):
        e = probe_env()
        if env:
            e.update(env)
        return subprocess.run([sys.executable, str(self.PROBE), *args],
                              capture_output=True, text=True, cwd=cwd, env=e)

    def _project(self, tmp: str, config: dict) -> Path:
        root = Path(tmp)
        (root / ".asset-check.json").write_text(json.dumps(config))
        img = root / "hero-banner.jpg"
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=2600x1000:d=1:r=1",
               "-frames:v", "1", str(img))
        return img

    def test_project_config_changes_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._project(tmp, {
                "global": {"hard_max_width_px": 3000},
                "image_categories": {"banner-desktop": {"max_width_px": 3000}},
            })
            out = self._run(str(img), cwd=tmp)
        self.assertIn("**Compliant**", out.stdout,
                      f"project override did not apply:\n{out.stdout}{out.stderr}")

    def test_override_is_disclosed_in_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._project(tmp, {
                "global": {"hard_max_width_px": 3000},
                "image_categories": {"banner-desktop": {"max_width_px": 3000}},
            })
            out = self._run(str(img), cwd=tmp)
        self.assertIn("local override", out.stdout,
                      "a verdict reached via local config must say so, or a reader "
                      "cannot tell it from the team standard")

    def test_no_overrides_restores_the_team_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._project(tmp, {
                "global": {"hard_max_width_px": 3000},
                "image_categories": {"banner-desktop": {"max_width_px": 3000}},
            })
            out = self._run("--no-overrides", str(img), cwd=tmp)
        self.assertIn("Non-compliant", out.stdout,
                      "--no-overrides must ignore local config so CI can gate on the "
                      "team standard regardless of what a project set")

    def test_custom_category_resolves_from_its_filename_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".asset-check.json").write_text(json.dumps({
                "image_categories": {"lookbook": {
                    "max_width_px": 1200, "max_bytes": 409600,
                    "preferred_format": "jpg"}},
                "filename_hints": {"lookbook": ["lookbook"]},
            }))
            img = Path(tmp) / "lookbook-spread.jpg"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=900x600:d=1:r=1",
                   "-frames:v", "1", str(img))
            out = self._run(str(img), cwd=tmp)
        self.assertIn("category: `lookbook`", out.stdout,
                      f"custom category not picked up:\n{out.stdout}{out.stderr}")
        self.assertIn("**Compliant**", out.stdout)

    def test_category_above_global_cap_is_rejected(self):
        """Silently-ineffective config is worse than none: the global cap is checked
        first, so a higher category limit would never apply."""
        with tempfile.TemporaryDirectory() as tmp:
            img = self._project(tmp, {
                "image_categories": {"banner-desktop": {"max_width_px": 3000}},
            })
            out = self._run(str(img), cwd=tmp)
        self.assertEqual(out.returncode, 2, out.stdout)
        self.assertIn("could never take effect", out.stderr)
        self.assertIn("hard_max_width_px", out.stderr,
                      "the error should name the fix, not just the problem")

    def test_malformed_config_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".asset-check.json").write_text("{ not json")
            img = Path(tmp) / "x.jpg"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=100x100:d=1:r=1",
                   "-frames:v", "1", str(img))
            out = self._run(str(img), cwd=tmp)
        self.assertEqual(out.returncode, 2)
        self.assertIn("not valid JSON", out.stderr)

    def test_config_is_found_from_a_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp, {
                "global": {"hard_max_width_px": 3000},
                "image_categories": {"banner-desktop": {"max_width_px": 3000}},
            })
            deep = Path(tmp) / "assets" / "banners"
            deep.mkdir(parents=True)
            img = deep / "hero-banner.jpg"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=2600x1000:d=1:r=1",
                   "-frames:v", "1", str(img))
            out = self._run("hero-banner.jpg", cwd=str(deep))
        self.assertIn("**Compliant**", out.stdout,
                      "config should be found by walking up, since assets usually "
                      "live in a subfolder")

    def test_comment_keys_are_not_counted_as_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp, {"_comment": "brand notes", "notes": "more notes"})
            out = self._run("--show-config", cwd=tmp)
        self.assertIn("No values differ", out.stdout,
                      f"documentation keys must not count as rule changes:\n{out.stdout}")

    def test_env_var_config_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "brand.json"
            cfg.write_text(json.dumps({
                "global": {"hard_max_width_px": 3000},
                "image_categories": {"banner-desktop": {"max_width_px": 3000}},
            }))
            img = Path(tmp) / "hero-banner.jpg"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=2600x1000:d=1:r=1",
                   "-frames:v", "1", str(img))
            out = self._run(str(img), cwd=tmp,
                            env={"ASSET_CHECK_CONFIG": str(cfg)})
        self.assertIn("**Compliant**", out.stdout, out.stdout + out.stderr)

    def test_json_output_reports_config_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._project(tmp, {
                "global": {"hard_max_width_px": 3000},
                "image_categories": {"banner-desktop": {"max_width_px": 3000}},
            })
            out = self._run("--json", str(img), cwd=tmp)
        data = json.loads(out.stdout)
        self.assertIn("config", data)
        self.assertIn("global.hard_max_width_px", data["config"]["overridden"])
        self.assertEqual([s["layer"] for s in data["config"]["sources"]],
                         ["team", "project"])

    def test_overrides_do_not_affect_the_guidelines_verifier(self):
        """The bundled thresholds remain the team contract; a local override must not
        make verify-guidelines think the doc and JSON disagree."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".asset-check.json").write_text(json.dumps({
                "global": {"hard_max_width_px": 9000}}))
            out = subprocess.run(
                [sys.executable, str(SCRIPTS / "verify-guidelines.py")],
                capture_output=True, text=True, cwd=tmp, env=probe_env(),
            )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


# --------------------------------------------------------------------------
# Second audit pass. Every case here is a way local config could produce a
# wrong answer without saying anything — the failure mode that matters most,
# because a silently mis-graded asset looks exactly like a correct one.
# --------------------------------------------------------------------------
class TestConfigBoundary(unittest.TestCase):
    """The config search must not escape the project."""

    def _img(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        img = directory / "hero-banner.jpg"
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=4000x1000:d=1:r=1",
               "-frames:v", "1", str(img))
        return img

    def _run(self, img: Path, cwd: Path, home: Path):
        return subprocess.run([sys.executable, str(SCRIPTS / "probe.py"), img.name],
                              capture_output=True, text=True, cwd=str(cwd),
                              env=probe_env(home))

    LOOSE = {"global": {"hard_max_width_px": 9999},
             "image_categories": {"banner-desktop": {"max_width_px": 9999}}}

    def test_config_above_a_git_boundary_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".asset-check.json").write_text(json.dumps(self.LOOSE))
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            img = self._img(repo / "assets")
            out = self._run(img, img.parent, root)
        self.assertIn(
            "Non-compliant", out.stdout,
            "a config outside the repository was applied — one stray file in a parent "
            f"directory would silently loosen limits for every repo under it:\n{out.stdout}",
        )

    def test_config_at_the_repo_root_is_found_from_a_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / ".asset-check.json").write_text(json.dumps(self.LOOSE))
            img = self._img(repo / "assets" / "banners")
            out = self._run(img, img.parent, Path(tmp))
        self.assertIn("**Compliant**", out.stdout,
                      f"config at the repo root should still apply:\n{out.stdout}")

    def test_config_in_home_is_not_treated_as_a_project_config(self):
        """cwd is *below* $HOME with no repo marker, so the walk would reach $HOME.

        A config there must not act as a project config: the project layer outranks
        the user layer, so it would silently beat ~/.claude/asset-check/config.json
        for every directory on the machine.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True)
            (home / ".asset-check.json").write_text(json.dumps(self.LOOSE))
            img = self._img(home / "scratch" / "work")   # under $HOME, no .git
            out = self._run(img, img.parent, home)
        self.assertIn(
            "Non-compliant", out.stdout,
            "a config in $HOME was applied with project-level precedence to an "
            f"unrelated directory beneath it:\n{out.stdout}",
        )


class TestAmbiguousCategoryIsDisclosed(unittest.TestCase):
    """Overriding hint_priority can silently re-route bundled categories."""

    def _run(self, img: Path, cwd: Path):
        return subprocess.run([sys.executable, str(SCRIPTS / "probe.py"), img.name],
                              capture_output=True, text=True, cwd=str(cwd),
                              env=probe_env())

    def _thumb(self, directory: Path) -> Path:
        img = directory / "product-thumb.jpg"
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=400x400:d=1:r=1",
               "-frames:v", "1", str(img))
        return img

    def test_priority_override_that_changes_a_category_is_disclosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".asset-check.json").write_text(json.dumps({
                "hint_priority": ["product-image", "banner-desktop",
                                  "product-thumbnail", "banner-mobile"]}))
            img = self._thumb(root)
            out = self._run(img, root)
        combined = out.stdout + out.stderr
        self.assertIn(
            "hint_priority", combined,
            "an override silently re-graded a 400px thumbnail as a product image, "
            "which then fails as 'too small, not auto-fixable'. The tool knows both "
            f"answers and should say so:\n{combined}",
        )
        self.assertIn("product-thumbnail", combined,
                      "the disclosure should name the category the baseline would use")

    def test_no_disclosure_when_there_is_no_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            img = self._thumb(root)
            out = self._run(img, root)
        self.assertNotIn("hint_priority", out.stdout + out.stderr,
                         "default runs must stay quiet — a warning that always fires "
                         "is a warning nobody reads")
        self.assertIn("category: `product-thumbnail`", out.stdout)


class TestGlobalByteCapIsEnforced(unittest.TestCase):
    """global.hard_max_bytes is a mandatory rule; it must behave like the width cap."""

    def _run(self, *args, cwd):
        return subprocess.run([sys.executable, str(SCRIPTS / "probe.py"), *args],
                              capture_output=True, text=True, cwd=str(cwd),
                              env=probe_env())

    @staticmethod
    def _heavy(directory: Path) -> Path:
        """Noise does not compress, so this reliably lands over 1 MB."""
        img = directory / "art.jpg"
        ffmpeg("-f", "lavfi", "-i", "nullsrc=s=1900x1000,geq=random(1)*255:128:128",
               "-frames:v", "1", "-q:v", "1", str(img))
        return img

    def test_category_byte_limit_above_the_global_cap_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".asset-check.json").write_text(json.dumps({
                "image_categories": {"misc": {"max_bytes": 52428800}}}))
            img = self._heavy(root)
            out = self._run(img.name, cwd=root)
        self.assertEqual(out.returncode, 2,
                         f"a byte limit that the global cap overrides should be "
                         f"rejected, as the width equivalent already is:\n{out.stdout}")
        self.assertIn("hard_max_bytes", out.stderr,
                      "the error should name the global field to raise")

    def test_raising_both_limits_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".asset-check.json").write_text(json.dumps({
                "global": {"hard_max_bytes": 52428800},
                "image_categories": {"misc": {"max_bytes": 52428800}}}))
            img = self._heavy(root)
            out = self._run(img.name, cwd=root)
        self.assertNotIn("| FAIL |", out.stdout,
                         f"raising both should be coherent:\n{out.stdout}")

    def test_default_config_cites_the_global_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            img = self._heavy(root)
            out = self._run(img.name, cwd=root)
        self.assertIn("global cap", out.stdout,
                      f"an over-1MB file should cite the mandatory global limit:\n{out.stdout}")


class TestExitCodePrecedence(unittest.TestCase):
    """A definite failure is more actionable than 'could not verify'."""

    def _run(self, *args, cwd):
        return subprocess.run([sys.executable, str(SCRIPTS / "probe.py"), *args],
                              capture_output=True, text=True, cwd=str(cwd),
                              env=probe_env())

    def test_definite_failure_outranks_unverifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            bad = root / "hero-banner.jpg"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=2600x1000:d=1:r=1",
                   "-frames:v", "1", str(bad))
            missing = root / "gone.jpg"          # probe failure -> the 2 class
            out = self._run(bad.name, missing.name, cwd=root)
        self.assertEqual(
            out.returncode, 1,
            "exit 2 hid a real non-compliance behind an unrelated probe problem; a "
            "gate checking for 'assets failed' would have missed it",
        )

    def test_unverifiable_alone_still_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            out = self._run("gone.jpg", cwd=root)
        self.assertEqual(out.returncode, 2)


class TestCategoryFlagErrors(unittest.TestCase):
    def test_suppressed_category_explains_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".asset-check.json").write_text(json.dumps({
                "image_categories": {"lookbook": {
                    "max_width_px": 1200, "max_bytes": 409600,
                    "preferred_format": "jpg"}},
                "filename_hints": {"lookbook": ["lookbook"]}}))
            img = root / "a.jpg"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=900x600:d=1:r=1",
                   "-frames:v", "1", str(img))
            out = subprocess.run(
                [sys.executable, str(SCRIPTS / "probe.py"),
                 "--no-overrides", "--category", "lookbook", img.name],
                capture_output=True, text=True, cwd=str(root), env=probe_env())
        # Assert on an explanation, not on the word appearing in the usage line —
        # argparse prints "[--no-overrides]" in usage, which made a weaker
        # assertion pass while the message was still unhelpful.
        self.assertIn(
            "local config", out.stderr,
            "the category exists but was suppressed by --no-overrides; saying only "
            f"'invalid choice' sends the user hunting for a typo:\n{out.stderr}",
        )
        self.assertNotIn("invalid choice", out.stderr,
                         "replace the raw argparse error, don't just add to it")


class TestProgressOutput(unittest.TestCase):
    """--progress exists because a spinner cannot work here: inside a tool call
    stdout/stderr are not TTYs, so carriage-return frames are captured rather than
    rendered. Discrete lines stay readable and stream when backgrounded."""

    def _assets(self, tmp: str) -> list:
        root = Path(tmp)
        (root / ".git").mkdir()
        good = root / "product-thumb.jpg"
        bad = root / "hero-banner.jpg"
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=400x400:d=1:r=1", "-frames:v", "1",
               str(good))
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=2600x1000:d=1:r=1", "-frames:v", "1",
               str(bad))
        return [good, bad, root / "missing.jpg"]

    def _run(self, *args, cwd):
        return subprocess.run([sys.executable, str(SCRIPTS / "probe.py"), *args],
                              capture_output=True, text=True, cwd=str(cwd),
                              env=probe_env())

    def test_one_line_per_asset_including_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = self._assets(tmp)
            out = self._run("--progress", *[a.name for a in assets], cwd=tmp)
        lines = [l for l in out.stderr.splitlines() if l.startswith("[")]
        self.assertEqual(len(lines), 3,
                         f"expected a line per asset, including the probe failure:\n"
                         f"{out.stderr}")
        self.assertTrue(lines[0].startswith("[1/3]"), lines[0])
        self.assertTrue(lines[-1].startswith("[3/3]"), lines[-1])

    def test_progress_does_not_corrupt_json_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = self._assets(tmp)
            out = self._run("--progress", "--json", *[a.name for a in assets], cwd=tmp)
        data = json.loads(out.stdout)   # would raise if progress leaked to stdout
        self.assertEqual(len(data["results"]), 3)
        self.assertIn("[1/3]", out.stderr)

    def test_silent_without_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = self._assets(tmp)
            out = self._run(*[a.name for a in assets], cwd=tmp)
        self.assertNotIn("[1/3]", out.stderr,
                         "progress must be opt-in; a default that always chatters "
                         "becomes noise in the common single-asset case")

    def test_no_carriage_returns_or_ansi_escapes(self):
        """A spinner here would be captured, not rendered — so never emit one."""
        with tempfile.TemporaryDirectory() as tmp:
            assets = self._assets(tmp)
            out = self._run("--progress", *[a.name for a in assets], cwd=tmp)
        self.assertNotIn("\r", out.stderr,
                         "carriage returns are captured verbatim and collapse every "
                         "frame onto one unreadable line")
        self.assertNotIn("\x1b[", out.stderr, "ANSI escapes are not rendered either")


class TestFallbackCategoryIsDisclosed(unittest.TestCase):
    """`misc` has no filename hints, so reaching it always means nothing matched.

    That matters because misc carries the loosest limits in the config. Real CDN
    filenames rarely contain 'product', 'thumb' or 'banner', so catalogue assets land
    there routinely and a 1792 px grid thumbnail passes silently. The tool should say
    the category was a fallback rather than let it read as a considered match.
    """

    def _run(self, *args, cwd):
        return subprocess.run([sys.executable, str(SCRIPTS / "probe.py"), *args],
                              capture_output=True, text=True, cwd=str(cwd),
                              env=probe_env())

    def _asset(self, tmp: str, name: str, size: str = "1792x1792") -> Path:
        root = Path(tmp)
        if not (root / ".git").exists():
            (root / ".git").mkdir()
        img = root / name
        ffmpeg("-f", "lavfi", "-i", f"testsrc=size={size}:d=1:r=1", "-frames:v", "1",
               "-q:v", "8", str(img))
        return img

    def test_fallback_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._asset(tmp, "Roll_On_Duo_430x430_d106e95df1.jpg")
            out = self._run(img.name, cwd=tmp)
        self.assertIn("fallback", out.stdout.lower(),
                      f"nothing matched, so misc must not read as a considered "
                      f"choice:\n{out.stdout}")
        self.assertIn("--category", out.stdout,
                      "say how to correct it, not just that it happened")

    def test_matched_category_is_not_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._asset(tmp, "hero-banner.jpg", "1700x900")
            out = self._run(img.name, cwd=tmp)
        self.assertIn("category: `banner-desktop`", out.stdout)
        self.assertNotIn("fallback", out.stdout.lower(),
                         "a genuine hint match needs no caveat")

    def test_explicit_category_is_not_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._asset(tmp, "Roll_On_Duo_430x430.jpg")
            out = self._run("--category", "misc", img.name, cwd=tmp)
        self.assertNotIn("fallback", out.stdout.lower(),
                         "the user chose misc deliberately; do not second-guess it")

    def test_fallback_does_not_change_the_verdict(self):
        """Advisory only. Turning it into a failure would flag most CDN assets."""
        with tempfile.TemporaryDirectory() as tmp:
            img = self._asset(tmp, "Roll_On_Duo_430x430.jpg")
            out = self._run(img.name, cwd=tmp)
        self.assertIn("**Compliant**", out.stdout)
        self.assertEqual(out.returncode, 0)

    def test_json_records_how_the_category_was_chosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._asset(tmp, "Roll_On_Duo_430x430.jpg")
            out = self._run("--json", img.name, cwd=tmp)
        result = json.loads(out.stdout)["results"][0]
        self.assertEqual(result["category"], "misc")
        self.assertEqual(result.get("category_source"), "fallback")


class TestFaststartOnEncodes(unittest.TestCase):
    def test_every_mp4_producing_command_uses_faststart(self):
        """Without it the moov atom sits at the end, so nothing plays until the whole
        file has downloaded — which for a 19 MB clip on mobile data is the difference
        between 'slow' and 'broken'."""
        text = (SKILL / "references" / "video-fixes.md").read_text()
        blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
        offenders = [
            b.strip().splitlines()[0]
            for b in blocks
            if "ffmpeg" in b and "_optimised.mp4" in b and "+faststart" not in b
        ]
        self.assertEqual(
            offenders, [],
            "these commands write an MP4 without +faststart:\n  " +
            "\n  ".join(offenders),
        )


class TestReferenceCoverage(unittest.TestCase):
    """Knowledge the skill demonstrably needs, and re-derived from scratch when the
    reference did not carry it."""

    def test_cdn_resize_by_url_is_documented(self):
        """The cheapest fix for an oversized CDN image is no fix at all — request a
        smaller variant. The skill reconstructed this by measuring the live CDN,
        which means it was missing from the reference."""
        text = (SKILL / "references" / "image-fixes.md").read_text()
        for needle, why in (
            ("?width=", "the resize parameter"),
            ("quality=", "the quality parameter"),
            ("re-upload", "that no re-upload is needed"),
        ):
            self.assertIn(needle, text, f"image-fixes.md should document {why}")

    def test_low_end_android_targets_are_documented(self):
        text = (SKILL / "references" / "video-fixes.md").read_text()
        for needle, why in (
            ("baseline", "the safest H.264 profile for cheap decoders"),
            ("-bf 0", "disabling B-frames"),
            ("-an", "dropping audio when the source is silent"),
            ("getMaxSupportedInstances", "the concurrent-decoder limit"),
        ):
            self.assertIn(needle, text.lower() if needle.islower() else text,
                          f"video-fixes.md should document {why}")

    def test_the_documented_low_end_command_produces_baseline_no_bframes(self):
        """Run what the reference tells people to run, and check the output really is
        what it claims — a profile claim nobody verifies is just folklore."""
        text = (SKILL / "references" / "video-fixes.md").read_text()
        self.assertIn("-profile:v baseline", text)
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "in.mp4", Path(tmp) / "out.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=1280x720:d=1:r=60",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src))
            out = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(src),
                 "-vf", "scale=854:-2:flags=lanczos,format=yuv420p",
                 "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
                 "-bf", "0", "-b:v", "700k", "-maxrate", "1M", "-bufsize", "1M",
                 "-an", "-movflags", "+faststart", str(dst), "-y"],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            probed = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=profile,has_b_frames,width",
                 "-of", "csv=p=0", str(dst)],
                capture_output=True, text=True).stdout.strip()
        self.assertIn("Baseline", probed, f"got {probed}")
        self.assertIn("854", probed, f"got {probed}")
        self.assertTrue(probed.endswith(",0") or ",0," in probed,
                        f"B-frames should be 0: {probed}")


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required to run these tests")
    unittest.main(verbosity=2)

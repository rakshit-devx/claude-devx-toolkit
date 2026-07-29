# Contributing

The point of this repo is that a fix discovered once is available to everyone. That
only works if changes land here rather than in a local install.

## Why local edits don't count

Installed plugins live in a cache (`~/.claude/plugins/cache/...`) that is overwritten
whenever the marketplace updates. Editing `SKILL.md` or `thresholds.json` there is
invisible to teammates and disappears on the next `/plugin marketplace update`. Always
change the repo.

## Is this a PR at all?

Not every rule belongs here. A brand or project that legitimately differs from the
baseline should use a local `.asset-check.json` override — see
[`references/customising.md`](plugins/asset-check/skills/asset-check/references/customising.md).
That needs no review and survives plugin updates.

Open a PR when the change is a **shared truth**:

| Situation | Where |
|---|---|
| Our brand needs a wider hero than the standard | local override |
| An asset type only this project has | local override |
| The team number is simply wrong | **PR** |
| A working fix for a failure mode others will hit | **PR** |
| Three repos now carry the same override | **PR** — the baseline is wrong |

That last row is the signal worth watching. Repeated identical overrides mean the
baseline has drifted from reality, and adding a fourth copy is not the fix.

## Where a change belongs

| Change | Edit |
|---|---|
| A limit moved (max width, size budget, bitrate) | `references/thresholds.json` |
| A new asset category | `references/thresholds.json` — the category, plus `filename_hints` and its slot in `hint_priority` |
| A newly solved failure mode | the matching `references/*-fixes.md` |
| A new automated check | `grade_image` / `grade_video` in `scripts/probe.py` |
| Workflow or reporting behaviour | `SKILL.md` |

Paths are under `plugins/asset-check/skills/asset-check/`.

Never restate a threshold in prose. Numbers live in `thresholds.json` and are read
from there, so the docs can't drift out of sync with what the script enforces.

## Changing a limit

Two files hold the same numbers: [`asset-guidelines.md`](plugins/asset-check/skills/asset-check/references/asset-guidelines.md),
which people read and which is the authority, and `thresholds.json`, which the tooling
enforces.

Edit the markdown first, then update `thresholds.json`, then run:

```bash
python3 plugins/asset-check/skills/asset-check/scripts/verify-guidelines.py
```

It reports exactly which field disagrees and exits non-zero, so you do not have to
remember what you missed. It checks every category's minimum, preferred range, maximum
width, hard cap, preferred and maximum file size, and preferred format; every video
setting; the `format_required` flag on the SVG-mandatory categories; the global width
cap; and the mandatory-rule count.

Changing only the JSON leaves the team following a document the tooling contradicts.
Changing only the markdown means nothing is enforced. The verifier catches both, which
is why the rule is "run the verifier" rather than "remember to update both".

## Adding a fix

Add an `### Issue N` block to the relevant fixes file with four parts:

1. **Symptom** — the exact ffprobe field or value that identifies it, so the next
   person can recognise it rather than guess.
2. **Why it matters** — the user-visible consequence. "Washed out on mobile" tells
   someone whether their problem matches; "wrong colour range" doesn't.
3. **Fix** — the working command, complete enough to copy.
4. **Why this command** — the reasoning. This is the part that matters most: a bare
   command gets cargo-culted into situations it doesn't suit. The HDR fix in
   `video-fixes.md` is the example to follow — it explains why retagging beats tone
   mapping, which is what stops someone "improving" it back into a tone map.

## Adding a category

In `thresholds.json`:

1. Add the entry under `image_categories` with `min_width_px`,
   `preferred_width_px`, `max_width_px`, `max_bytes`, `preferred_format`, and
   `allowed_formats`. Add `format_required: true` if the preferred format is mandatory
   (as with SVG for icons and logos).
2. Add filename tokens under `filename_hints`.
3. Insert it into `hint_priority` at the right specificity. **Order matters:**
   categories are tested in this order and the first filename match wins. A specific
   category must sit above any broader one whose tokens it overlaps —
   `product-thumbnail` above `product-image`, or `product-thumb.jpg` gets graded as a
   full product image and fails for being under 1400 px.

## Testing a change

**Run the suite first — it is the fastest way to know you broke something:**

```bash
python3 tests/test_asset_check.py
```

Every test there pins a real defect. Add one for whatever you fix; the bugs worth
guarding against are the ones whose output looked fine, so a passing eyeball is not
evidence. If you change grading behaviour deliberately, update the test in the same
commit and say why in the message.

For exploratory checks, generate fixtures with ffmpeg — no need to commit binaries:

```bash
# oversized banner
ffmpeg -f lavfi -i "testsrc=size=2600x1000:d=1:r=1" -frames:v 1 hero-banner.jpg -y

# HDR HLG 4K
ffmpeg -f lavfi -i "testsrc=size=3840x2160:d=1:r=30" -c:v libx264 -pix_fmt yuv420p \
  -colorspace bt2020nc -color_primaries bt2020 -color_trc arib-std-b67 hdr-4k.mp4 -y

# full-range luma
ffmpeg -f lavfi -i "testsrc=size=1280x720:d=1:r=30" -c:v mjpeg -pix_fmt yuvj420p fullrange.mp4 -y
```

Then check the verdicts and the exit code:

```bash
cd plugins/asset-check/skills/asset-check
python3 scripts/probe.py hero-banner.jpg hdr-4k.mp4 fullrange.mp4; echo "exit=$?"
python3 scripts/probe.py --json hero-banner.jpg | python3 -m json.tool > /dev/null && echo "JSON ok"
```

Two traps worth generating a fixture for, because both produce output that looks fine:

- **`testsrc` compresses extremely well**, so it cannot produce a genuinely
  over-bitrate video no matter what `-b:v` you pass. Use a noise source when testing
  the bitrate check:
  `-f lavfi -i "nullsrc=s=1920x1080:d=3:r=30,geq=random(1)*255:128:128"`.
- **Category inference** is filename-driven, so any change to `hint_priority` or
  `filename_hints` can silently reroute existing assets. Check a spread of names
  before and after.

If you touch `probe.py`, verify all three kinds still grade: a raster image, an SVG
(parsed separately — ffprobe reports SVG as 0×0, which would otherwise pass
everything), and a video.

## Constraints to preserve

- **`probe.py` stays stdlib-only.** No pip dependencies — a teammate should be able to
  run it immediately after installing ffmpeg. This is also why SVG geometry is read
  with a regex rather than an XML parser: SVGs can arrive from untrusted URLs and the
  stdlib XML parsers are open to entity-expansion attacks.
- **ffmpeg stays sufficient.** ImageMagick and `cwebp` may be *preferred* for a given
  fix but must never be *required* — every failure mode needs a working ffmpeg path.
- **Cross-platform.** No `sips` or other macOS-only tools; the team isn't all on Mac.
- **Never overwrite source assets.** Output goes to `<name>_optimised.<ext>`.
- **Everything the plugin needs lives under `plugins/asset-check/`.** Files outside it
  are not packaged on install, so a reference at the repo root is missing for every
  teammate who installs the plugin — which is why `asset-guidelines.md` sits in
  `references/` rather than a top-level `docs/`.
- **Never let an unreadable check report as a pass.** `probe.py` distinguishes *absent*
  metadata from *unreadable* metadata and emits `UNKN` for the latter. Collapsing the
  two is how a remote HDR video came to be reported as SDR-compliant.

## Releasing

Bump `version` in both `.claude-plugin/marketplace.json` and
`plugins/asset-check/.claude-plugin/plugin.json`, then merge. Teammates pick it up
with:

```
/plugin marketplace update claude-devx-toolkit
```

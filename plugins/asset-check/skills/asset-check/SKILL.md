---
name: asset-check
description: Check image and video assets against team compliance rules and optimise the ones that fail. Use this whenever assets are being added, reviewed, or uploaded — banners, product images, thumbnails, icons, logos, backgrounds, or MP4/MOV video — and whenever someone mentions an image or video being too large, too heavy, slow to load, blurry, washed out, wrong format, or crashing the app. Also use for questions about correct dimensions, file size budgets, JPG vs PNG vs SVG vs WebP, video bitrate, codec, HDR, or colour space, and when asked to compress, resize, convert, or optimise any image or video. Trigger on a local asset path, an asset URL (Shopify CDN included), or a vague "check the new assets".
---

# Asset Check

Grade image and video assets against the team's compliance rules, then fix the ones
that fail.

Two things make this reliable rather than a judgement call each time:

- **`references/thresholds.json` holds every limit.** Do not restate numbers from
  memory or hardcode them into commands — read them from the file, or let `probe.py`
  apply them. The human-readable guidelines behind it are
  `references/asset-guidelines.md`, beside it; read that when someone asks *why* a
  limit exists or wants the rules explained rather than an asset checked.
- **`probe.py` does the grading.** Eyeballing whether 1.9 MB is acceptable for a
  mobile banner produces different answers from different people on different days.
  The script produces the same verdict every time.

### Running the bundled scripts

Every command below is written `$SKILL/…`. Set that once per session, because your
working directory is the user's project, not this skill — a bare `scripts/probe.py`
resolves against their repo and fails with *No such file or directory*:

```bash
SKILL="${CLAUDE_PLUGIN_ROOT:-<this skill's base directory>}/skills/asset-check"
```

`CLAUDE_PLUGIN_ROOT` is set when this runs as an installed plugin. If it is empty, use
the absolute base directory reported when this skill loaded.

---

## Step 0 — Confirm the tooling (first run only)

```bash
bash "$SKILL/scripts/check-deps.sh"
```

Only `ffmpeg`, `ffprobe`, and `python3` are required. ImageMagick and `cwebp` are
optional — ffmpeg handles resize, recompression, and PNG→JPG flattening on its own.
If a required tool is missing the script prints per-platform install commands; relay
those and stop, since nothing below will work without them.

Skip this once you have seen it pass in the session.

---

## Step 1 — Probe and grade

```bash
python3 "$SKILL/scripts/probe.py" <asset> [<asset> ...]
```

Accepts local paths and `http(s)` URLs, mixed freely, as many as you like in one call
— it handles them together, so there is no reason to loop one file at a time.

Useful flags:

| Flag | Purpose |
|---|---|
| `--category <name>` | Force an image category instead of inferring from the filename |
| `--json` | Machine-readable output, for chaining or bulk summaries |
| `--list-categories` | Show the available image categories |
| `--no-overrides` | Ignore user/project config and grade against team thresholds |
| `--show-config` | Show which config layers are active and what they changed |

Exit codes: `0` all compliant · `1` at least one non-compliant · `2` a probe failed **or
a check could not be verified**. `1` outranks `2`, so a real failure is never hidden
behind an unreadable file in the same run.

**Resolving the input:**

- **Local path or URL** → pass it straight through; type is detected by extension.
- **"check the new assets" / no argument** → find candidates first, then pass them in:
  ```bash
  git status --porcelain --untracked-files=all | grep -iE '\.(jpg|jpeg|png|webp|svg|mp4|mov)$'
  ```
  In a non-git folder, use `ls -lt` and take the recently modified assets.
- **Category** → inferred from the filename (`hero-banner.jpg` → desktop banner,
  `product-thumb.jpg` → thumbnail). The inference is deliberate about overlaps, but
  it is still a guess from a filename. When the category materially changes the
  verdict and the name is ambiguous, ask rather than assume — grading a thumbnail as
  a product image demands 1400 px of a 400 px file and produces a nonsense failure.

---

## Step 2 — Report

Show the table `probe.py` produced. It already contains the per-check requirement,
actual value, status, and a remedy for anything failing — reformatting it adds nothing
and risks transcribing a number wrong.

Then state the verdict per asset:

- **Compliant** — nothing to do.
- **Compliant, with warnings** — within limits but not ideal. Say what and why; do not
  push a fix the rules don't require.
- **Non-compliant** — name each failing check and what it needs.
- **Could not fully verify** — one or more checks came back `UNKN`. Never round this up
  to "looks fine". It happens most often on remote video, where colour metadata may not
  arrive over HTTP, so HDR genuinely cannot be determined. Say plainly which checks are
  unanswered and that confirming them needs the file downloaded and re-probed locally.

When you ask someone to change an asset, give the reason alongside the number.
`global.rationale` in `thresholds.json` has them: quality consistency across web and
mobile, load performance, **mobile-app memory pressure — the direct cause of crashes
and lag**, maintainability, and Shopify's limited storage. "This 3 MB banner will push
app memory up on low-end devices" gets acted on; "this exceeds 1 MB" gets argued with.

Call out anything marked **not auto-fixable** explicitly, because these need a person,
not a command:

- An image **below its category minimum** — upscaling is precisely what the rules
  forbid, so this needs a larger source from the designer.
- A **raster icon or logo** where SVG is required — nothing here can vectorise a
  raster image, and auto-tracing produces worse output than the PNG.

---

## Step 3 — Optimise, after asking

**Ask before encoding.** Optimisation writes new files and is a judgement call about
acceptable quality loss — that belongs to the person who owns the asset. Show the
verdict, say what you would run, and wait.

Then read the fix reference for the media type and follow it:

- **`references/image-fixes.md`** — resize, recompress, PNG→JPG (including the
  composite-on-white trap), WebP, and why raster→SVG is a dead end.
- **`references/video-fixes.md`** — bitrate, full-range `yuvj420p`, HDR/HLG, HEVC,
  MOV remuxing, frame rate.

Non-negotiables when applying fixes, because each has a specific failure mode:

- **Write to `<name>_optimised.<ext>` beside the original. Never overwrite the
  source** — it is the only thing a retry can start from.
- **Resize before compressing.** An oversized banner is oversized because of its
  dimensions. Lowering quality to hit a size budget yields a blurry asset that is
  still the wrong size.
- **Never upscale.** Every resize command in the references is shrink-only.
- **Never tone map HDR.** It desaturates badly and is not recoverable. Scale and
  retag to bt709 instead — `references/video-fixes.md` explains why that preserves
  colour.

---

## Step 4 — Verify

Re-probe the output. An encode that ran without error still may not have produced a
compliant file:

```bash
python3 "$SKILL/scripts/probe.py" "<name>_optimised.jpg" --category <same-category>
```

Check the fix didn't trade one failure for another — resizing to satisfy a width limit
can push an image under its category minimum, which is worse than where you started.

For the colour fixes in particular (`yuvj420p`, HDR), tell the user to actually look
at the result on a device. Those failures pass every automated check while looking
obviously wrong on a phone, which is how they reached production in the first place.

---

## Working across many assets

- Pass every asset to one `probe.py` call rather than looping.
- Use `--json` for bulk work and summarise: how many compliant, and the failures
  grouped by cause. A thirty-row table nobody reads is not a report.
- Run encodes concurrently with `&` and `wait`, keeping concurrency near the core
  count — x264 is already multi-threaded, so launching twenty at once makes them
  contend rather than finish sooner.

---

## Brand and project specific rules

The bundled thresholds are the team baseline. A brand, sub-brand, or project often has
legitimate reasons to differ — wider editorial art, a tighter size budget for a
performance-critical surface, an asset type the baseline has no category for.

**When someone tells you a rule that isn't in the baseline, persist it.** A rule you
only remember for the rest of the conversation is a rule they have to repeat next week.
Write it to `.asset-check.json` in their project root and `probe.py` picks it up on
every future run, for them and for anyone else working in that repo:

```json
{
  "_comment": "Why these differ from the team baseline — write this down, the next
               person will ask.",
  "global": { "hard_max_width_px": 3000 },
  "image_categories": {
    "banner-desktop": { "max_width_px": 3000 },
    "lookbook": { "max_width_px": 1200, "max_bytes": 409600, "preferred_format": "jpg" }
  },
  "filename_hints": { "lookbook": ["lookbook", "editorial"] },
  "notes": "Practices that aren't thresholds. Read these back before advising."
}
```

Then confirm it applied:

```bash
python3 "$SKILL/scripts/probe.py" --show-config
```

Things worth knowing so this behaves predictably:

- **Layering** is team baseline → `~/.claude/asset-check/config.json` (that person,
  every project) → the project's `.asset-check.json` (that repo, everyone). Later
  layers win. Use the user layer for a personal preference, the project layer for a
  brand rule — a brand rule belongs to the repo, not to whoever happened to set it up.
- **The project config is found by walking up, but only within the project.** The
  search stops at the first `.git` (or `package.json`, `pyproject.toml`, …) and never
  looks at `$HOME` or above, so a stray file in a parent directory cannot quietly
  re-grade unrelated repos. A config in `$HOME` is ignored — put machine-wide
  preferences in the user layer, which is what it is for.
- **If a `hint_priority` override changes which category an asset gets**, the run says
  so and names what the baseline would have used. Reordering that list can turn a
  compliant 400 px thumbnail into a "too small, not auto-fixable" failure, and that is
  not something to discover from a confusing report.
- **These files sit outside the plugin on purpose.** Anything inside it is replaced by
  `/plugin marketplace update`, which is exactly how the previous
  edit-my-own-instructions approach lost everything it learned.
- **A new category needs a `filename_hints` entry** to be inferred automatically;
  otherwise it is only reachable via `--category`.
- **Raising a category above `global.hard_max_width_px` is rejected**, because the
  global cap is checked first and the override could never take effect. The error says
  which global value to raise.
- **Overrides are disclosed** in every report and in `--json`. Don't suppress that: a
  reader who sees a pass needs to know whether it met the team standard or a local one.
- **`--no-overrides` grades against the team baseline** regardless of local config.
  That's the right flag for CI, and the right way to answer "would this pass for
  everyone, not just us?"

**Not everything belongs in local config.** If the rule is a team-wide truth — a
genuine correction to the shared standard, or a fix that would help everyone — that is
a PR against the toolkit repo, not a local override. Local config is for legitimate
difference; the repo is for shared truth. When it's ambiguous, ask which they mean
rather than quietly choosing.

Full schema and worked examples: `references/customising.md`.

---

## Adding a new fix

When you hit and solve a failure mode that isn't documented here, it belongs in the
repo so the next person doesn't rediscover it. This skill is installed from a shared
marketplace, so editing these files in place does nothing — the plugin cache is
overwritten on update and your teammates never see it.

Open a PR against the toolkit repo instead:

1. Add an `### Issue N` block to the relevant `references/*-fixes.md` with the
   symptom, why it matters, the working command, and why that command is the right
   one. The reasoning is the part that survives; a bare command gets
   cargo-culted into the wrong situation.
2. If a limit changed, edit `references/thresholds.json` — not the prose.
3. If it warrants a new check, extend `grade_image` / `grade_video` in
   `scripts/probe.py`, and add a regression test under `tests/`.

See `CONTRIBUTING.md` in the repo root.

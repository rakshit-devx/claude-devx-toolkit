# claude-devx-toolkit

Shared [Claude Code](https://claude.com/claude-code) plugins for the Foxtale
engineering and design team.

| Plugin | What it does |
|---|---|
| **asset-check** | Grades image and video assets against the team's compliance rules and optimises the ones that fail. |

---

## Install

Add the marketplace, then install the plugin:

```
/plugin marketplace add <owner>/claude-devx-toolkit
/plugin install asset-check@claude-devx-toolkit
```

> Replace `<owner>/claude-devx-toolkit` with the repo path once this is pushed. Until
> then, add it from a local clone:
>
> ```
> /plugin marketplace add /absolute/path/to/claude-devx-toolkit
> ```

Verify it landed:

```
/plugin
```

`asset-check` should appear as installed, exposing both the auto-triggering skill and
the `/asset-check` command.

---

## Requirements

`asset-check` needs **ffmpeg**, **ffprobe**, and **python3**. Nothing else, and no
`pip install` — the probe script is stdlib only.

```bash
# macOS
brew install ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg

# Fedora
sudo dnf install ffmpeg

# Windows
winget install Gyan.FFmpeg     # or work inside WSL
```

Optional extras — **ImageMagick** for marginally better image resampling, **cwebp**
for WebP output. Neither is required; ffmpeg covers resize, recompression, and
PNG→JPG flattening on its own.

```bash
brew install imagemagick webp          # macOS
sudo apt install imagemagick webp      # Debian / Ubuntu
```

Check your machine at any time:

```bash
bash plugins/asset-check/skills/asset-check/scripts/check-deps.sh
```

Note that many ffmpeg builds — including current Homebrew — ship **without** the WebP
encoder. The dep check reports this, and WebP output routes through `cwebp` instead.

---

## Using asset-check

It triggers on its own when you mention assets. All of these work:

- "check these banners before I upload them"
- "this hero image is 3 MB, can you sort it out"
- "why does this video look washed out on the phone"
- "what size should a mobile banner be"

Or invoke it explicitly:

```
/asset-check ./assets/hero-banner.jpg
/asset-check https://cdn.shopify.com/s/files/.../banner.jpg
/asset-check                      # checks recently added assets
```

You can also run the grader directly, without Claude:

```bash
cd plugins/asset-check/skills/asset-check
python3 scripts/probe.py ~/Downloads/hero-banner.jpg
python3 scripts/probe.py --json ~/Downloads/*.jpg
python3 scripts/probe.py --list-categories
```

Exit codes are `0` compliant, `1` non-compliant, `2` probe failed — so it drops into
CI or a pre-commit hook as-is.

### What it checks

**Images** — width against per-category minimums and maximums, file size, and format.
Nine categories (product image, thumbnail, desktop/mobile banner, background, UI and
illustrative icons, logo, misc), inferred from the filename or forced with
`--category`.

**Video** — resolution, codec, bitrate, frame rate, pixel format, colour space, HDR,
and container. Every video limit is a maximum; lower is always fine.

All limits live in
[`plugins/asset-check/skills/asset-check/references/thresholds.json`](plugins/asset-check/skills/asset-check/references/thresholds.json).
That file is what the tooling enforces — change a number there and every teammate's
verdict changes with it.

### Source of truth

`thresholds.json` is a transcription of the team's published guidelines, kept in
[`docs/source/`](docs/source) so the provenance travels with the repo:

- [`image_asset_guidelines.pdf`](docs/source/image_asset_guidelines.pdf) — the nine
  image categories, their preferred and maximum sizes, and the six mandatory rules.
- [`video_asset_guidelines.png`](docs/source/video_asset_guidelines.png) — the video
  settings table.

Every value in `thresholds.json` has been verified against these documents. If the
guidelines change, update the document *and* `thresholds.json` in the same PR — the
docs are the authority, the JSON is what runs.

The guidelines exist for concrete reasons, carried in `thresholds.json` under
`global.rationale`: consistent quality across web and mobile, faster loading, **reduced
mobile-app memory use — which is what prevents crashes and lag**, maintainable asset
management, and efficient use of Shopify's limited storage. Quote the reason when
asking someone to fix an asset; it lands better than a number.

### What it deliberately won't do

- **Upscale.** An image below its category minimum is reported as non-compliant and
  not auto-fixable. Upscaling is what the rules exist to prevent.
- **Vectorise a raster icon.** Nothing can turn a PNG logo into a real SVG;
  auto-tracing looks worse than the PNG. It tells you to get the vector source.
- **Tone map HDR.** It desaturates badly and is irreversible. HDR video is scaled and
  retagged to bt709 instead, which preserves the original colour.
- **Overwrite your originals.** Output always goes to `<name>_optimised.<ext>`.

---

## Repo layout

```
claude-devx-toolkit/
├── .claude-plugin/
│   └── marketplace.json           # marketplace manifest — lists the plugins
├── plugins/
│   └── asset-check/
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── commands/
│       │   └── asset-check.md     # /asset-check
│       └── skills/
│           └── asset-check/
│               ├── SKILL.md       # workflow (auto-triggering)
│               ├── references/
│               │   ├── thresholds.json    # all limits
│               │   ├── image-fixes.md
│               │   └── video-fixes.md
│               └── scripts/
│                   ├── probe.py           # probe + grade
│                   └── check-deps.sh
├── CONTRIBUTING.md
└── README.md
```

Adding another plugin later means a new folder under `plugins/` and an entry in
`marketplace.json` — teammates get it via `/plugin marketplace update`, with no
re-onboarding.

---

## Publishing

This repo is local for now. To make it available to the team:

```bash
cd claude-devx-toolkit

# private to the org (recommended — these are internal standards)
gh repo create foxtale-data-product/claude-devx-toolkit --private --source=. --push

# or public
gh repo create foxtale-data-product/claude-devx-toolkit --public --source=. --push
```

Then update the install snippet above with the real repo path and share it. Teammates
need read access to the repo; `/plugin marketplace add` uses their own git
credentials.

---

## Contributing

Fixes and threshold changes go through a PR — see [CONTRIBUTING.md](CONTRIBUTING.md).
Editing an installed plugin in place does nothing: the cache is overwritten on update
and nobody else sees the change.

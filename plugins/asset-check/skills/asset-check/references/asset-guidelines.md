# Asset Guidelines

Standards for uploading and managing image and video assets across web and mobile
platforms.

This document is the **human-readable authority**. `thresholds.json`, beside it, is
the machine-readable copy the tooling enforces, and
[`../scripts/verify-guidelines.py`](../scripts/verify-guidelines.py) asserts the two
agree — so they cannot drift apart unnoticed. Edit this file first, then run the
verifier and fix whatever it reports.

Both live inside the skill deliberately: files outside `plugins/asset-check/` are not
packaged when the plugin is installed, so a copy at the repo root would be missing for
everyone who installs it.

---

## Image Assets

The `Key` column is the value to pass to `probe.py --category`.

| Category | Key | Use Case | Preferred Size | Max Size | Format | Notes |
|---|---|---|---|---|---|---|
| Product Image | `product-image` | PDP main / zoom | 1400–1600 px | 2000 px (max 2400 px if required) | JPG | Maintain quality for zoom |
| Product Thumbnail | `product-thumbnail` | Listing / grid | 300–500 px, 50–150 KB | 800 px, 300 KB | JPG | Must load instantly |
| Collection / Pages Banner (Desktop) | `banner-desktop` | Collection, homepage, promo banners | 1600–1800 px, 300–700 KB | 2500 px, 1 MB | JPG | Covers all large banners |
| Collection / Pages Banner (Mobile) | `banner-mobile` | Mobile banners (all types) | 800–1200 px, 200–400 KB | 1500 px, 700 KB | JPG | Separate from desktop |
| Background Images | `background` | Section backgrounds | 1400–1800 px, 300–600 KB | 2500 px, 1 MB | JPG | Avoid heavy usage |
| Icons (UI) | `icon-ui` | Buttons, small icons | 24–64 px, <20 KB | 128 px, 50 KB | SVG | Always prefer SVG |
| Icons (Illustrative) | `icon-illustrative` | Feature icons | 64–128 px, <30 KB | 256 px, 80 KB | SVG | Keep scalable |
| Logos | `logo` | Brand logos | 100–300 px, <50 KB | 500 px, 100 KB | SVG | Ensures sharpness |
| Misc Images | `misc` | Any other images | ≤1600 px, ≤500 KB | 2000 px, 1 MB | JPG | Size based on screen area covered (larger display → higher res, smaller card → lower res) |

All sizes are widths in pixels. "KB" and "MB" are binary (1 KB = 1024 bytes).

---

## Mandatory Rules

- Preferred size is ALSO the minimum required size — do NOT upload smaller images
  (upscaling will break UI quality).
- Always use JPG for photos/images (use PNG only when transparency is required).
- Always use SVG for icons and logos.
- Keep image file size under 1 MB (do not reduce quality to achieve this — images must
  remain sharp with no visible blur or pixelation).
- Never upload images wider than 2500 px.
- Always create separate mobile and desktop banners.

## This Ensures

- Consistent visual quality across web and mobile (no pixelation or blur).
- Faster loading and better app/web performance.
- Reduced memory usage in mobile apps (avoids crashes and lag).
- Cleaner and more maintainable asset management.
- Efficient use of storage (prevents overuse of limited storage in Shopify).

Quote the relevant reason when asking someone to change an asset. "This will raise app
memory on low-end devices" gets acted on; "this exceeds 1 MB" gets argued with.

---

## Video Assets

**Every value is a maximum. Lower is always acceptable.** High-end video crashes the
mobile app or exhausts device RAM, so a 4K 60 fps HDR master is not a better asset
here — it is a broken one.

| Setting | Value |
|---|---|
| Resolution | 720p (preferred) or 1080p (max) |
| Codec | H.264 |
| Bitrate | 3–5 Mbps (max) |
| FPS | 30 (preferred), 60 (max) |
| Pixel format | yuv420p |
| Color space | bt709 |
| HDR | disabled |
| Container | MP4 |

Resolution limits are orientation-agnostic: portrait 1080×1920 is as acceptable as
landscape 1920×1080. The constraint is that the long edge stays within 1920 and the
short edge within 1080.

---

## Notes on interpretation

A few points that come up repeatedly and are worth stating explicitly:

- **Preferred size is a floor as well as a target.** An image below its category
  minimum is non-compliant and cannot be fixed by upscaling — that is the specific
  thing these limits exist to prevent. It needs a larger source.
- **SVG for icons and logos is mandatory, not preferred**, despite the "Always prefer
  SVG" wording. A raster icon cannot be converted to a real SVG; auto-tracing produces
  worse output than the original PNG. Request the vector from the designer.
- **Resize before compressing.** An oversized image is oversized because of its
  dimensions. Dropping quality to hit a file-size budget yields a blurry image that is
  still the wrong size.
- **For Shopify uploads, compress lightly** (quality 90–100). The CDN re-compresses on
  delivery, so pre-compressing hard causes double-compression artifacts — you pay the
  quality cost twice and save nothing.
- **Never tone map HDR video.** It desaturates badly and is irreversible. Scale and
  retag to bt709 instead; HLG is SDR-backwards-compatible, so the pixel values already
  display correctly and only the metadata needs correcting.

# Image Fixes

Every command here is written for **ffmpeg first**, because ffmpeg is already
required for video probing and is the one tool guaranteed to be present. ImageMagick
produces marginally better resampling and is offered as an optional upgrade, but no
fix in this file *depends* on it — a teammate with only ffmpeg installed can resolve
every image problem below.

Two rules that shape all of these:

- **Resize before you compress.** An oversized image is oversized because of its
  dimensions, not its quality setting. Dropping quality on a 2600 px banner to hit
  a size budget gives you a blurry 2600 px banner.
- **Never upscale.** Every resize below is shrink-only. If an image is under the
  minimum width for its category, no command fixes it — go get a larger source.

Write output to `<name>_optimised.<ext>` beside the original. Never overwrite the
source; the original is the only thing you can retry from.

---

## Quality settings

ffmpeg's JPEG quality is `-q:v`, on an inverted 2–31 scale (2 is best). Measured on
a 2400 px test image:

| `-q:v` | Approx. equivalent | Use for |
|---|---|---|
| 2 | ~95 | Product images, zoomable assets |
| 3 | ~90 | Banners, backgrounds, Shopify uploads |
| 5 | ~80 | General web imagery |
| 7 | ~70 | Thumbnails, where instant load beats fidelity |

For **Shopify uploads use `-q:v` 2–3**. Shopify's CDN re-compresses on delivery, so
pre-compressing hard causes double-compression artifacts — you pay the quality cost
twice and save nothing.

`-map_metadata -1` strips EXIF, reliably saving 5–20 KB on camera-originated photos
and removing any embedded location data.

---

## Fix 1 — Image exceeds max width

Shrink-only resize. `min(TARGET,iw)` is what makes it shrink-only: if the source is
already narrower than the target, its own width wins and nothing is scaled.

```bash
ffmpeg -i "<input>" \
  -vf "scale='min(<TARGET_WIDTH>,iw)':-1:flags=lanczos" \
  -q:v 3 -map_metadata -1 \
  "<name>_optimised.jpg" -y
```

`-1` lets height follow the aspect ratio. `lanczos` is the sharpest of the practical
resamplers, which matters when downscaling detailed product photography.

ImageMagick equivalent, if available:

```bash
magick "<input>" -resize <TARGET_WIDTH>x\> -quality 90 -strip "<name>_optimised.jpg"
```

The `\>` suffix is ImageMagick's shrink-only flag — omitting it will happily upscale.
On ImageMagick 6 the command is `convert` rather than `magick`; v7 renamed it, and
`convert` is deprecated there.

---

## Fix 2 — File size too large, dimensions already fine

Recompress without touching dimensions:

```bash
ffmpeg -i "<input>" -q:v 5 -map_metadata -1 "<name>_optimised.jpg" -y
```

For thumbnails, where load speed is the whole point:

```bash
ffmpeg -i "<input>" -q:v 7 -map_metadata -1 "<name>_optimised.jpg" -y
```

If a single quality step doesn't get under budget, prefer resizing (Fix 1) over
pushing `-q:v` past 7. Past that point the artifacts become visible on the fine
detail that product imagery lives on.

---

## Fix 3 — PNG that should be JPG

Only convert when the PNG has no meaningful transparency. If it does, the correct
answer is usually to keep it as PNG.

Transparency must be composited onto white explicitly. ffmpeg's default is to
composite onto **black**, which silently darkens the asset — this is the most common
way this conversion goes wrong:

```bash
ffmpeg -i "<input>.png" \
  -filter_complex "color=c=white[c];[c][0:v]scale2ref[bg][img];[bg][img]overlay,format=yuv420p" \
  -frames:v 1 -update 1 -q:v 3 \
  "<name>_optimised.jpg" -y
```

`scale2ref` sizes the white background to the source, so this works at any dimensions
without hardcoding them. `-update 1` is required for single-image output; without it
ffmpeg treats the output as an image sequence and errors on the pattern.

Verify the result is composited on white rather than black:

```bash
ffmpeg -v error -i "<name>_optimised.jpg" -vf "scale=1:1" -f rawvideo -pix_fmt rgb24 - | xxd -p
```

A washed-light value confirms white. Near-black bytes mean the overlay didn't apply.

ImageMagick equivalent:

```bash
magick "<input>.png" -background white -alpha remove -alpha off -quality 90 "<name>_optimised.jpg"
```

---

## Fix 4 — Resize and compress together

The common case for an oversized banner:

```bash
ffmpeg -i "<input>" \
  -vf "scale='min(<TARGET_WIDTH>,iw)':-1:flags=lanczos" \
  -q:v 3 -map_metadata -1 \
  "<name>_optimised.jpg" -y
```

Same as Fix 1 — resizing a large image usually solves the file size on its own, so
try this before reaching for lower quality.

---

## Fix 5 — Raster icon or logo that should be SVG

**This is not fixable with these tools, and pretending otherwise makes things worse.**
Neither ffmpeg nor ImageMagick can vectorise a raster image; auto-tracing a logo
produces wrong curves and muddy edges that look worse than the PNG did.

Say so plainly and route it back:

> This icon/logo is a raster image (PNG/JPG). SVG is required so it stays sharp at
> every screen density. Please request the vector source from the designer — it will
> exist in the original design file.

If shipping a PNG is genuinely unavoidable in the interim, at minimum bring it inside
the category's width and size limits with Fix 1 or Fix 2, and flag it as debt rather
than resolved.

---

## Fix 6 — WebP output

WebP is a meaningful saving over JPG at matched quality, but check availability
first: **many ffmpeg builds ship without the WebP encoder** (including the current
Homebrew build), so `cwebp` is the dependable path.

```bash
cwebp -q 82 -resize <TARGET_WIDTH> 0 "<input>" -o "<name>_optimised.webp"
```

The `0` height means "derive from the aspect ratio". Quality 82 is a good default;
WebP holds up better than JPG at equivalent numbers.

Before choosing WebP, confirm every surface that consumes the asset actually renders
it. Shopify serves WebP automatically from JPG sources, so uploading WebP there is
usually redundant work.

---

## Fix 7 — Oversized image already on the Shopify CDN

**Check this before reaching for ffmpeg.** If the asset is served from
`cdn.shopify.com`, the cheapest fix is usually no fix: request a smaller variant by
URL. Nothing is re-encoded, nothing is re-uploaded, and the stored original stays
available for surfaces that genuinely need the full size.

```
https://cdn.shopify.com/.../hero.jpg?width=430&quality=80
```

Measured against a live 1792×1792 file:

| Request | Delivered | Payload |
|---|---|---|
| as-is | 1792×1792 | 176 KB |
| `?width=430&quality=80` | 430×430 | **28 KB** |
| `?width=800&quality=80` | 800×800 | 69 KB |
| `?width=860&quality=80` (2× for retina) | 860×860 | 77 KB |

The older suffix form still works — `.../hero_430x.jpg` returns 430×430 — but at 33 KB
against 28 KB, because it gives no control over quality. Prefer the query parameters.

**Shopify already negotiates WebP.** Requesting that JPG with `Accept: image/webp`
returns `content-type: image/webp`, so uploading a WebP yourself is redundant work on
this CDN.

**Why this matters more than the byte count:** the download is the smaller half. A
1792 px image decodes to a **12.8 MB** ARGB bitmap in memory; at 430 px it is **739
KB**. In a grid of twenty cards that difference is the gap between a smooth scroll and
an OOM kill on a low-end device — which is why the width rule exists separately from
the file-size rule.

**Check whether the app is already doing this** before advising anything. In this
codebase `getOptimizedImageURL(url, resize, quality)` appends or replaces `width` and
`quality`, and `BaseImage` applies it on delivery — so an image whose *stored*
dimensions look oversized may be perfectly fine at the point of render. Confirm which
surface it renders on before recommending a change.

So the decision is:

- **Renders through a CDN-resizing path** → nothing to do; the stored size is
  irrelevant.
- **Rendered at full stored size** → non-compliant, and either the surface should
  request a width or the source needs resizing (Fix 1).

---

## Verifying a fix

Always re-probe rather than trusting the encode:

```bash
python3 scripts/probe.py "<name>_optimised.jpg" --category <category>
```

The exit code is 0 when compliant, so this composes into scripts. Check that the
fix didn't trade one failure for another — resizing to satisfy a width limit can
push an image below its category minimum, which is a worse outcome than the
original problem.

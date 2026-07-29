# Video Fixes

Every requirement for video is a **maximum**, not a target. Lower is always
acceptable. The reason is blunt: high-end video crashes the mobile app or exhausts
device RAM. A 4K 60 fps HDR master is not a better asset for this pipeline, it is a
broken one.

Write output to `<name>_optimised.mp4` beside the original. Never overwrite the source.

---

## Issue 1 — Bitrate above 5 Mbps

**Symptom:** `bit_rate` exceeds 5,000,000.

```bash
ffmpeg -i "<input>" \
  -c:v libx264 -b:v 4M -maxrate 5M -bufsize 5M \
  -pix_fmt yuv420p -color_range tv \
  -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  -c:a aac -b:a 160k -movflags +faststart \
  "<name>_optimised.mp4" -y
```

`-bufsize` bounds how far the encoder may drift above the target between rate
checks; without it `-maxrate` is only loosely honoured.

---

## Issue 2 — Pixel format `yuvj420p` (full / PC range)

**Symptom:** `pix_fmt: yuvj420p`, usually with `color_range: pc`.

**Why it matters:** full-range luma spans 0–255 where TV range spans 16–235. Mobile
players assume TV range, so full-range content renders washed out and low-contrast on
device even though it looks correct on a desktop editor.

```bash
ffmpeg -i "<input>" \
  -vf "scale=in_range=pc:out_range=tv,format=yuv420p" \
  -c:v libx264 -pix_fmt yuv420p -color_range tv \
  -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  -c:a copy -movflags +faststart \
  "<name>_optimised.mp4" -y
```

The range conversion has to happen in the filter chain. Tagging `-color_range tv`
alone relabels the file without remapping the values, which makes the output *more*
wrong — now the metadata lies about the pixels.

---

## Issue 3 — HDR video (bt2020 / HLG / Dolby Vision), often 4K

**Symptom:** `color_space: bt2020nc`, `color_transfer: arib-std-b67` (HLG) or
`smpte2084` (PQ), or a `DOVI` entry in `side_data_list`. Usually paired with 4K.

**Why it matters:** HDR renders broken or crashes outright on SDR mobile displays.

> **Do not tone map.** `zscale=t=linear:npl=100`, the `tonemap` filter, Hable,
> Reinhard — all of them visibly desaturate the footage and shift hues. The output
> looks obviously worse than the source and the colour damage is not recoverable.

**Fix — scale and retag, leaving the pixel values alone:**

```bash
ffmpeg -i "<input>" \
  -map 0:v:0 -map 0:a:0? \
  -vf "scale=1080:1920:flags=lanczos,format=yuv420p" \
  -c:v libx264 -b:v 4M -maxrate 5M -bufsize 5M \
  -color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  -c:a aac -b:a 160k -movflags +faststart \
  "<name>_optimised.mp4" -y
```

**Why this works:** HLG was designed to be backwards-compatible with SDR. The pixel
values already display correctly on an SDR screen — the only actual problem is the
metadata telling the player to expect HDR. Retagging to bt709 fixes the instruction
without touching the picture, which is why it preserves colour where tone mapping
destroys it.

Adjust `scale=1080:1920` to the real aspect ratio — that value is portrait. For
landscape use `scale=1920:1080`, or `scale='min(1920,iw)':-2` to stay shrink-only and
orientation-agnostic. Use `-2` rather than `-1` for video: H.264 requires even
dimensions and `-2` rounds to the nearest even number.

`-map 0:v:0 -map 0:a:0?` is essential for MOV and Dolby Vision sources. They often
carry multiple video tracks (or a DV enhancement layer), and without explicit mapping
ffmpeg may select the wrong one or fail on the stream layout.

The trailing `?` on the audio map makes it optional. Without it, ffmpeg aborts with
`Stream map '' matches no streams` on any video that has no audio track — which
promotional and product clips very often do not.

---

## Issue 4 — Wrong codec (HEVC / H.265) or wrong container (MOV)

**Symptom:** `codec_name: hevc`, or the file is `.mov`.

Re-encode to H.264 in MP4 using the Issue 3 command. Always include
`-map 0:v:0 -map 0:a:0?` for MOV sources.

If the codec is already H.264 and only the container is wrong, remux instead of
re-encoding — it is instant and lossless:

```bash
ffmpeg -i "<input>.mov" -c copy -movflags +faststart "<name>_optimised.mp4" -y
```

`-movflags +faststart` moves the moov atom to the front so playback can begin before
the whole file downloads. Every command here includes it, because without it a viewer
waits for the entire file before the first frame — on a 19 MB clip over mobile data
that is the difference between slow and broken.

Note that `ffprobe` reports the container as the whole demuxer family
(`mov,mp4,m4a,3gp,3g2,mj2`) for both MP4 and MOV, so it cannot tell you which one you
have. Trust the file extension.

---

## Issue 5 — Colour space untagged

**Symptom:** no `color_space`, `color_transfer`, or `color_primaries` in the ffprobe
output.

**Assessment:** SDR H.264 at standard resolutions is bt709 by convention, and players
treat it that way. Flag it as unspecified but treat it as compatible — a re-encode
purely to add tags costs a generation of quality for no visible gain. If you are
re-encoding for another reason anyway, include the bt709 tags then.

---

## Issue 6 — Frame rate above 60 fps

**Symptom:** `r_frame_rate` above 60.

```bash
ffmpeg -i "<input>" -r 30 \
  -c:v libx264 -b:v 4M -maxrate 5M -bufsize 5M \
  -pix_fmt yuv420p -color_range tv \
  -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  -c:a copy -movflags +faststart \
  "<name>_optimised.mp4" -y
```

30 fps is preferred for this pipeline. Dropping from 120 or 240 fps also cuts bitrate
substantially, so this often resolves an over-bitrate finding at the same time.

---

## Batching

ffprobe reads URLs directly, so remote assets need no download step. Run encodes
concurrently — they are CPU-bound and independent:

```bash
for f in *.mov; do
  ffmpeg -v error -i "$f" -map 0:v:0 -map 0:a:0? \
    -vf "scale='min(1920,iw)':-2:flags=lanczos,format=yuv420p" \
    -c:v libx264 -b:v 4M -maxrate 5M -bufsize 5M \
    -color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
    -c:a aac -b:a 160k -movflags +faststart \
    "${f%.*}_optimised.mp4" -y &
done
wait
```

Keep concurrency near your core count. Every x264 encode is already multi-threaded,
so launching twenty at once makes them contend rather than finish sooner.

---

## Verifying a fix

Re-probe rather than assuming the encode did what you asked:

```bash
python3 scripts/probe.py "<name>_optimised.mp4"
```

Confirm specifically that HDR now reads SDR, `pix_fmt` is `yuv420p`, colour space is
`bt709`, and bitrate landed under 5 Mbps. Then watch it — a technically compliant file
can still be visually wrong, and the colour failures in Issues 2 and 3 are exactly the
kind that pass every automated check while looking obviously bad on a phone.

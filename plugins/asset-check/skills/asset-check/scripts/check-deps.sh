#!/usr/bin/env bash
# Report which asset tools are available and which optimisation paths that unlocks.
#
# Only ffmpeg/ffprobe are required. ImageMagick and cwebp are genuinely optional —
# ffmpeg covers resize, recompress and PNG->JPG flattening on its own. This script
# exists so a teammate finds out up front, rather than halfway through an encode.
#
# Exit 0 = ready to work. Exit 1 = a required tool is missing.

set -uo pipefail

missing_required=0

have() { command -v "$1" >/dev/null 2>&1; }

report() { # name status detail
  printf '  %-12s %-14s %s\n' "$1" "$2" "$3"
}

echo "asset-check dependencies"
echo

# ---- required -------------------------------------------------------------
if have ffprobe; then
  report ffprobe "present" "$(ffprobe -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
else
  report ffprobe "MISSING" "required — all probing depends on it"
  missing_required=1
fi

if have ffmpeg; then
  report ffmpeg "present" "$(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
else
  report ffmpeg "MISSING" "required — all encoding depends on it"
  missing_required=1
fi

if have python3; then
  report python3 "present" "$(python3 --version 2>&1)"
else
  report python3 "MISSING" "required — probe.py needs it (stdlib only, no pip installs)"
  missing_required=1
fi

echo

# ---- optional -------------------------------------------------------------
if have magick; then
  report ImageMagick "present" "v7 'magick' — preferred for image resizing"
elif have convert; then
  report ImageMagick "present" "v6 'convert' — works, but v7 'magick' is preferred"
else
  report ImageMagick "absent" "optional — ffmpeg handles resize/compress/flatten instead"
fi

if have cwebp; then
  report cwebp "present" "WebP encoding available"
else
  report cwebp "absent" "optional — only needed to produce WebP"
fi

# ffmpeg is often built without the WebP encoder, so check rather than assume.
if have ffmpeg; then
  if ffmpeg -hide_banner -encoders 2>/dev/null | grep -qE '^\s*V[^ ]*\s+libwebp'; then
    report "ffmpeg webp" "present" "ffmpeg can also write WebP"
  else
    report "ffmpeg webp" "absent" "this ffmpeg build cannot write WebP — use cwebp"
  fi
fi

echo

if [ "$missing_required" -ne 0 ]; then
  cat <<'INSTALL'
Install the missing required tools:

  macOS      brew install ffmpeg
  Debian     sudo apt install ffmpeg
  Fedora     sudo dnf install ffmpeg
  Windows    winget install Gyan.FFmpeg     (or use WSL)

Optional extras:

  macOS      brew install imagemagick webp
  Debian     sudo apt install imagemagick webp
INSTALL
  exit 1
fi

echo "Ready — required tools present."
exit 0

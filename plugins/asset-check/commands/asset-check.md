---
description: Check image and video assets for compliance and optimise non-compliant ones. Takes a URL, a local file path, or nothing to check recently added assets.
argument-hint: "[url | file-path | 'new'] [category]"
allowed-tools: Bash(python3:*), Bash(bash:*), Bash(ffprobe:*), Bash(ffmpeg:*), Bash(cwebp:*), Bash(magick:*), Bash(convert:*), Bash(ls:*), Bash(stat:*), Bash(file:*), Bash(curl:*), Bash(git status:*), Read, Glob
---

Use the **asset-check** skill to check and optimise assets.

Target: `$ARGUMENTS`

If that is empty, check recently added or modified assets in the working directory.

Follow the skill's workflow — probe and grade with `scripts/probe.py`, report the
table it produces, and ask before optimising anything.

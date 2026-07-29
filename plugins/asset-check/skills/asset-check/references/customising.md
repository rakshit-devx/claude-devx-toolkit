# Customising for a brand or project

The bundled thresholds are the team baseline. Real brands differ from a baseline, so
you can override any of it locally — without editing the plugin and without a PR.

Overrides live **outside** the plugin. That is the whole point: anything inside it is
replaced wholesale by `/plugin marketplace update`, so a rule stored there would
silently vanish on the next update.

---

## Where config goes

| Layer | Path | Scope | Use it for |
|---|---|---|---|
| team | `references/thresholds.json` (bundled) | everyone | the shared standard — change via PR |
| user | `~/.claude/asset-check/config.json` | you, every project | a personal preference |
| project | `.asset-check.json` in the project root | that repo, everyone in it | a brand or product rule |

Later layers win. The project layer is found by walking up from the current directory,
so running from `assets/banners/` still picks up the config at the repo root.

**The walk is bounded by the project.** It stops at the first directory containing a
project marker — `.git`, `.hg`, `.svn`, `package.json`, `pyproject.toml`, `go.mod`,
`Cargo.toml`, `firebase.json` — and never examines `$HOME` or anything above it.

That bound is load-bearing. An unbounded search reaches the filesystem root, so one
stray file in a parent directory would silently re-grade every repository beneath it,
and a file in `$HOME` would become a machine-wide config carrying *project*
precedence — outranking the user layer that exists for exactly that purpose. So a
config at `$HOME` is ignored; put machine-wide preferences in the user layer.

`ASSET_CHECK_CONFIG=/path/to/file.json` overrides the project lookup entirely — useful
for testing a rule set, or for a monorepo where one brand's config lives elsewhere.

**Prefer the project layer for brand rules.** A brand rule belongs to the repository,
where it is committed, reviewed and shared. If it lives in one person's user config, the
same asset passes for them and fails for everyone else — which is worse than having no
rule at all.

---

## Common cases

### Loosen a limit

Our editorial hero art is intentionally wider than the standard banner:

```json
{
  "_comment": "Editorial heroes are shot wide; 2500 px crops the composition.",
  "global": { "hard_max_width_px": 3000 },
  "image_categories": { "banner-desktop": { "max_width_px": 3000 } }
}
```

Both are needed. The global cap is evaluated before any per-category limit, so raising
only the category would never take effect — and rather than half-apply that, `probe.py`
rejects it and tells you which global value to raise.

The same applies to file size. `global.hard_max_bytes` enforces the mandatory "keep
files under 1 MB" rule ahead of any category limit, so a heavier category needs both:

```json
{
  "global": { "hard_max_bytes": 2097152 },
  "image_categories": { "background": { "max_bytes": 2097152 } }
}
```

### Tighten a limit

A performance-critical surface where the baseline is too generous:

```json
{
  "_comment": "PLP thumbnails are the first paint on mobile; 100 KB is our budget.",
  "image_categories": { "product-thumbnail": { "max_bytes": 102400 } }
}
```

### Reorder category inference

`hint_priority` decides which category wins when a filename matches more than one.
Reordering it is legitimate, but it changes how *bundled* categories resolve too, and
the consequences are not obvious: putting `product-image` above `product-thumbnail`
makes `product-thumb.jpg` grade as a product image, which then fails its 1400 px
minimum as *not auto-fixable* — a compliant 400 px thumbnail reported as a broken
asset.

Because that is hard to spot from the report alone, any override that changes an
asset's category is called out on the run:

```
note: hint_priority override graded product-thumb.jpg as 'product-image';
      the team baseline would use 'product-thumbnail'. Pass --category to be explicit.
```

Adding a new category does **not** require touching `hint_priority` — categories absent
from it are still matched by their `filename_hints`. Reorder it only when you actually
need to change precedence.

### Add a category the baseline doesn't have

```json
{
  "image_categories": {
    "lookbook": {
      "max_width_px": 1200,
      "max_bytes": 409600,
      "preferred_format": "jpg"
    }
  },
  "filename_hints": { "lookbook": ["lookbook", "editorial"] }
}
```

`max_width_px`, `max_bytes` and `preferred_format` are required — those cannot be
guessed, and inventing a default would mean grading assets against a number nobody
chose. Everything else is derived:

| Field | Default |
|---|---|
| `allowed_formats` | same as `preferred_format` |
| `min_width_px` | `0` |
| `preferred_width_px` | `[min_width_px, max_width_px]` |
| `label` | the key, title-cased |
| `use_case` | `"custom"` |

The `filename_hints` entry is what lets the category be inferred automatically. Without
it the category still works, but only when passed explicitly via `--category lookbook`.

### Make a format mandatory

```json
{
  "image_categories": {
    "logo": { "preferred_format": ["svg"], "format_required": true }
  }
}
```

`format_required` turns a preference into a hard failure and marks it *not
auto-fixable*, which is correct for anything needing a vector source.

### Record a practice that isn't a threshold

Not every rule is a number. `notes` is free text and is read back when advising:

```json
{
  "notes": [
    "Our CDN strips EXIF on upload, so don't bother stripping it locally.",
    "PNG is allowed for pack shots — the transparent background is deliberate.",
    "Video for the app ships portrait; landscape masters need a re-crop, not a resize."
  ]
}
```

---

## Checking what's active

```bash
python3 "$SKILL/scripts/probe.py" --show-config
```

```
Active configuration, lowest precedence first:

  team     .../references/thresholds.json
  user     ~/.claude/asset-check/config.json
  project  /repo/.asset-check.json

4 value(s) overridden from the team defaults:
  - global.hard_max_width_px
  - image_categories.banner-desktop.max_width_px
  - image_categories.lookbook
  - filename_hints.lookbook
```

Comment keys (`_comment`, `notes`, `$schema`) are not counted as overrides — otherwise
the number would be meaningless.

Every report also carries a provenance line whenever overrides are in play, and
`--json` includes a `config` block with the active layers and the overridden paths. Do
not hide either: a reader who sees a pass needs to know whether it cleared the team
standard or a local one.

---

## Grading against the team baseline

```bash
python3 "$SKILL/scripts/probe.py" --no-overrides <assets>
```

This ignores both local layers. Use it in CI, and whenever the question is "would this
pass for everyone?" rather than "does this pass for us?".

It is also the honest way to audit your own overrides — run both and compare. If a lot
of assets only pass locally, the baseline and your brand have drifted apart, and that is
worth a conversation rather than more overrides.

---

## Merge semantics

- **Objects merge** recursively, so `{"image_categories": {"logo": {"max_bytes": N}}}`
  changes only that one field and leaves the rest of `logo` intact.
- **Scalars and arrays replace** outright. A partially merged list of formats or hints
  is never what anyone means, so `allowed_formats` and `filename_hints` entries are
  wholesale replacements.
- **Unrecognised top-level keys warn** rather than fail, so a typo surfaces instead of
  doing nothing quietly.
- **Malformed JSON is a hard error** with the parse position. A config that cannot be
  read must not degrade to "no config" — that would silently grade against the wrong
  rules.

---

## When to upstream instead

Local config is for legitimate difference. It is the wrong tool for:

- A **correction** to the shared standard — if the team number is wrong, fix it for
  everyone via PR.
- A **newly solved failure mode** — a working ffmpeg fix for a problem others will hit
  belongs in `references/*-fixes.md` in the repo.
- A rule **every** project ends up copying. If three repos carry the same override, the
  baseline is wrong; change the baseline.

See `CONTRIBUTING.md` in the toolkit repo.

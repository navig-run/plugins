# navig-dedupe

> A free, **standalone** perceptual duplicate finder for photos, video, audio, and any file —
> that installs **without** navig, and also lights up inside it as **`navig dedupe`**.
> Non-destructive by design: extras are **quarantined** (moved), never deleted.

Four engines, all lifted out of navig-core so there is **one source of truth**
(`navig-core/navig/media/*_dedupe.py` is now a thin re-export shim — the same
`voice/` → navig-audio pattern):

| Mode | What it catches | Needs |
|------|-----------------|-------|
| **images** | Re-saved / resized / re-encoded photo copies (256-bit dHash) + redundant `X_thumb` thumbnails | numpy, Pillow |
| **files** | Exact byte duplicates of **any** file type (SHA-256, size/head pre-sieved) | — (stdlib) |
| **audio** | The same track re-encoded across formats (Chromaprint acoustic fingerprint) | `fpcalc` binary |
| **video** | Re-uploaded / re-encoded clips (evenly-spaced keyframe dHash) | `ffmpeg` / `ffprobe` |

## Install & use (standalone)

```bash
pip install navig-dedupe            # numpy + Pillow come with it
navig-dedupe scan ~/Pictures        # dry run: report duplicate groups (nothing touched)
navig-dedupe scan ~/Pictures --near --move ~/dupes             # quarantine resized copies
navig-dedupe restore ~/dupes        # changed your mind? move everything back
ndup scan ./downloads --all --json  # all four modes, machine-readable output
# also: python -m navig_dedupe --help
```

Default (no mode flags) scans **images + files** — the two that need no external binaries.
The largest / highest-resolution file in each group is always the one **kept**. A file caught
### It will not re-flag its own quarantine

`--move` writes copies into a quarantine dir, and every one of them is byte-identical to
the original it was quarantined for. A later scan of the parent would report the whole
quarantine as fresh duplicates. Quarantine-shaped folder names (`.trash`, `_dupes`,
`_quarantine`, …) are therefore skipped when they sit *inside* the scanned tree — point
the root at one directly and it is scanned normally. Add your own with `--exclude NAME`.

### How `files` mode stays fast on a big library

A file whose **size** is unique cannot have a byte-identical twin, so it is never opened.
Two sieves run before any hashing — size (`stat`, no read at all), then the first 64 KB —
and only what survives both is hashed in full. Both sieves are exact, not heuristic:
identical files always share a size and a head, so no duplicate is ever missed.

On a real 29,055-file / 193 GB music library this took the full-hash pass from **193 GB
to 2.1 GB read** — 93× less — and produced byte-for-byte the same clusters.

by more than one mode (e.g. an exact-duplicate photo) is counted and moved **once**, not per
mode. Add **`-v`** to list the actual duplicate filenames per group, and **`--fail-on-dupes`**
to make a dry run exit non-zero (drop `navig-dedupe scan assets/ --fail-on-dupes` into CI to
fail a build that ships duplicate assets). Tip: keep the `--move` dir **outside** the scanned folder, or run the same `--move`
consistently — either way an already-quarantined file is never re-scanned.

### Undo is always available

`scan --move` writes a `.dedupe-restore.json` manifest into the quarantine dir, so nothing is
ever a one-way trip:

```bash
navig-dedupe restore ~/dupes              # move every quarantined file back where it came from
navig-dedupe restore ~/dupes --to ~/Pics  # …or into a different root
```

A file whose original path has since been re-occupied is left safely in quarantine and reported —
`restore` never overwrites.

## Inside navig

The same command surface is `navig dedupe scan <folder> …`. It ships as the optional
`navig[dedupe]` extra and registers a free **Dedupe** module tile. The agent can invoke it
from natural language ("clean up duplicate photos in this folder") via the bundled skill.

## Develop

```bash
cd navig-dedupe
python -m pip install -e ".[dev]"
navig-dedupe --help
pytest -q
```

## Relationship to navig

`navig-dedupe` owns the code; `navig-core` re-exports it, so the two never fork. Inside navig it
is `navig dedupe`; the legacy `navig media dedupe-*` commands keep working via the shim.

Apache-2.0.

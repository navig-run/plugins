# navig-explore

NAVIG Explore module — a universal local media/data explorer for ANY folder. Buckets files by type (image/gif/video/audio/text/document/archive), auto-reads Telegram exports, and previews everything. A first-party navig plugin (free, toggleable).

**License:** Apache-2.0 · **Version:** 0.1.0

## What it is

`navig-explore` is a **first-party NAVIG plugin** — it extends NAVIG itself (CLI verbs +
gateway routes), and it is free and toggleable. Point it at any folder and it buckets the
files by type, previews them, and understands Telegram chat exports. Plugins are different
from *blocks*: a plugin adds capabilities to the platform; a block is an outcome you *apply*
with `navig apply`.

## Commands

```
navig explore <folder>              # bucket + preview any folder's files by type
navig explore audio-sort <dir>      # sort audio by CONTENT: music / voice / sfx
navig explore audio-undo <log>      # put an audio-sort run back exactly as it was
navig explore photos organize <dir> # file a photo library by EXIF capture date
navig explore photos undo <log>     # put a photos run back exactly as it was
navig telegram-exports <path>       # read a Telegram data export
```

### `photos organize` — date a photo library without wrecking it

Files photos into `<date-root>/YYYY/YYYY-MM/` from **EXIF capture dates**, quarantines
litter and undatable recovery salvage, and optionally moves video out of the photo tree.

```
navig explore probe   "X:\Photos"                    # capture dates (required)
navig explore dedup   "X:\Photos"                    # duplicate copies (optional)
navig explore photos organize "X:\Photos" \
    --video-dest "X:\Video\from-photos"              # PLAN only — review the CSV
navig explore photos organize "X:\Photos" --apply    # execute
```

`dedup` reports three classes, and only the first two are ever auto-quarantined:

| class | what it is | auto-trash |
|---|---|---|
| `exact` | byte-identical | yes |
| `identical-pixels` | **decodes to the same image**, differs only in container/metadata | yes |
| `near-image` / `near-video` | perceptually close (resize, crop, re-encode) | no — human decides |

`identical-pixels` is the class byte-hashing structurally cannot see: re-saving a JPEG or
writing one EXIF tag changes every byte while leaving every pixel untouched. On a real
family album, **184 of 210** name collisions were exactly this — SHA-256 alone would have
kept all 184 as `(1)` twins of photos already in the library. It costs no extra I/O (the
digest is taken during the decode the perceptual pass already performs), needs only a
decoder — not `imagehash` — and keeps the copy carrying the *most* metadata. Opt out with
`photos organize --no-pixel-dupes`, or skip the decode pass entirely with
`dedup --exact-only`.

**Acting on the report — `--quarantine`:**

```
navig explore dedup "X:\Photos"                 # report only
navig explore dedup "X:\Photos" --quarantine    # ...and move the redundant copies
navig explore undo  "<log printed above>"       # put every one of them back
```

Redundant copies move to `<root>/.trash/dupes/` keeping their relative path — nothing is
deleted. Only `exact` and `identical-pixels` are moved; a `near-image` cluster is often a
burst of consecutive shots rather than one photo twice, so those are always left for a
human. Overlapping groups are merged into connected components first, so a file that is
byte-identical to one copy *and* pixel-identical to a third resolves to exactly one
survivor in a single pass, and a group can never lose every copy.

What it refuses to do, and why it matters on a real archive:

- **Never flattens human-named folders.** A real date tree is not uniform — it holds
  `2009/Alzon 2009`, `2009/14 juillet 2009`, `2014/Берлин 2014`. Those names are the
  best organisation in the library and nothing can rebuild them. Only `YYYY-MM`
  buckets, `YYYY-00` stubs, `dd.mm.yy` day folders and folders you declare with
  `--loose` are ever reshaped; curated collections are read-only.
- **Never invents a date.** Capture dates come from EXIF only — never mtime. Archives
  assembled by recovery tools carry meaningless timestamps (that is how `1970` and
  `2031` folders appear). No capture date ⇒ the photo stays undated instead of being
  filed under a guess.
- **Never mistakes a caption for litter.** A zero-byte `.txt` is often the only
  description a folder has, with the filename as the note (`a la plage.txt`).
- **Never deletes.** Quarantine moves to `<root>/.trash/{junk,tiny,dupes}` keeping the
  original relative path, and every run writes an undo log *as it goes* — a run killed
  halfway is still fully reversible via `photos undo`.

Plan-by-default is the review gate: the first invocation only writes
`.mediaexplorer/photos-plan.csv`.

### `audio-sort` — sort a sound dump by what it *sounds* like

Ripped clips have useless names (`#fyp_#viral_7593….m4a`) and no tags, so bucketing by
type or filename can't tell a song from someone talking. `audio-sort` decides from the
waveform, using ffmpeg + numpy only — no model download, no GPU:

```
navig explore audio-sort "X:\Audio\TikTok\music" \
  --learn music="X:\Audio\Music\Genres" \
  --learn voice="X:\Audio\Spoken Word" \
  --learn sfx="X:\Audio\SFX\Foley Sound Effects" \
  --route music="X:\Audio\Production\Video Edit Music" \
  --route voice="X:\Audio\TikTok\voices" \
  --route sfx="X:\Audio\SFX\TikTok Sounds" \
  --min-conf 0.6 --apply
```

It trains on folders **you already sorted**, prints a cross-validated accuracy before
touching anything, and defaults to plan-only. Anything below `--min-conf` goes to
`_review/` rather than being guessed at, sidecars (`.md` transcripts) travel with their
audio, name collisions are suffixed rather than overwritten, and every move is logged so
`audio-undo` restores the folder exactly.

**Targeted cleanup.** `--min-dur` / `--max-dur` / `--keep` narrow what a run may touch,
so you can fix the misfits without reorganising the folder:

```
# pull the hour-long albums out of a sound-effects library,
# and leave the actual sound effects exactly where they are
navig explore audio-sort "X:\Audio\SFX" --model model.json \
  --route "music=X:\Audio\Music\Recovered from SFX" \
  --route "voice=X:\Audio\Spoken Word\Recovered from SFX" \
  --keep sfx --min-dur 120 --apply
```

Out-of-scope files are reported as `leave` in the plan rather than filtered out — a plan
that silently omitted them would look like it covered the folder when it deliberately
did not.

If `faster-whisper` is installed, its bundled Silero VAD adds speech-activity features —
the strongest single cue for "is someone talking?".

## Install

It ships in the NAVIG monorepo and loads automatically when NAVIG is installed.
To add/enable it explicitly:

```
navig plugin add navig-explore
```

Check wiring and toggle state:

```
navig store list
```

## Development

This package lives in the NAVIG monorepo under `plugins/navig-explore/`. It registers via
the `navig.plugins` entry-point group; CLI verbs register via `navig.commands`.

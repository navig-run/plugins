---
name: photos-vision
description: >-
  Search, organise and date a local photo library by what is actually IN the
  pictures — people, places, objects and scenes — instead of by EXIF metadata.
  Use when the user wants to find photos of a person, find photos of a thing
  ("photos of a red car on a beach"), work out when undated or data-recovery
  photos were taken, separate real photographs from screenshots and web junk, or
  organise a folder of recovered images. Works offline on the local GPU.
---

# Photo vision pass (`navig explore photos …`)

Adds person / place / object / date recognition on top of `navig explore probe`.
Everything is keyed on **content hash**, so labels survive reorganisation.
Indexing never modifies a photo; only `--apply` moves files, and every applied
run writes an undo log.

## Order of operations

```bash
navig explore probe          <root>          # 1. EXIF + dimensions (existing command)
navig explore photos index   <root>          # 2. hash, embed, classify   (~140 img/s)
navig explore photos faces   <root>          # 3. detect + embed faces     (~12 img/s)
navig explore photos dates   <root>          # 4. recover capture dates
navig explore photos places  <root>          # 5. locate (optional)
navig explore photos people  cluster <root>  # 6. group faces into people
```

Each step is resumable and idempotent; re-running plans no new work.

## Answering questions

```bash
# by object / scene / anything visible — multilingual
navig explore photos search "a red car on a beach" --root <root>
navig explore photos search "снег зимой" --root <root>

# by person (after naming a group)
navig explore photos people list <root>
navig explore photos people name <root> 31 "Anna"
navig explore photos search --root <root> --person Anna --year 2009

# more like this one
navig explore photos search --root <root> --like "X:\Photos\a.jpg"

# write the answer to a shareable page
navig explore photos search "birthday" --root <root> --gallery out.html
```

`people cluster` always over-splits one person into several groups — that is
inherent, not a misconfiguration. Fix it with
`people merge <root> <keep-id> <other-id>…`; named groups are locked and survive
later re-clustering.

## Organising

```bash
navig explore photos triage <root>            # plan only
navig explore photos triage <root> --apply    # then undo with triage-undo <log>
```

Separates `photo` / `webcam` / `screenshot` / `web-graphic` / `document` and
quarantines undecodable bytes. **Real photographs are never moved** — where a
photo belongs is the operator's decision. `--refile-dated` additionally files
photos into `By Date/YYYY/YYYY-MM`, but only when the recovered date's
confidence clears `--min-confidence` (default 0.80), which excludes folder-year
and folder-median guesses by design.

## Judging a result

```bash
navig explore photos explain <file>
```

Prints the derivation: the date, its `source` and `confidence`, the classes, the
faces and their rotation, and every other path holding the same bytes. Date
sources rank `exif` > `overlay` > `filename` > `twin` > `folder` > `sibling` >
`mtime`. Anything below 0.80 is a hint for a human, not grounds to move a file.

## What to know before trusting it

- **mtime is checked, not assumed.** If a library's mtimes disagree with its
  measured dates, the whole rung is disabled and reported — recovery tools stamp
  every file they write with the time of the recovery run.
- **Webcam frames often carry the date in the pixels.** The `webcam` class is
  OCR'd for a burned-in timestamp; that is a real date source where EXIF is gone.
- **Faces are searched at four rotations** when the upright pass is not
  confident, because salvaged photos lose their EXIF Orientation tag.
- **`--faces-engine arcface`** is more accurate on small/profile faces but its
  InsightFace model weights are licensed **non-commercial research only**. The
  default `yunet-sface` is Apache-2.0.

## Requirements

`pip install "navig-explore[vision]"` (torch, open_clip, opencv, scikit-learn).
A CUDA GPU is strongly preferred. Burned-in-timestamp OCR additionally needs
`tesseract` on PATH with the relevant language data.

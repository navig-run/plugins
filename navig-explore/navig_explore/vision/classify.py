"""Zero-shot content classes — "is this even a photograph?"

Recovery salvage is not a pile of photos. A carve of a browser cache yields
icons, sprites, stock imagery, poster art, UI screenshots and text-as-image
alongside the handful of pictures that actually matter. Running face clustering
or date inference over that mixture wastes the effort and pollutes the result,
so triage comes first.

Classification is nearly free: it reuses the SigLIP embedding the pipeline has
already computed and is a single matrix product against cached text prototypes.
No second decode, no second forward pass.

One class earns its place for a reason specific to this library: ``webcam``
frames carry a burned-in timestamp overlay, which is a *date source* for files
whose EXIF was destroyed. Finding them is how those dates get recovered.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Several prompts per class, averaged — a single prompt is brittle, and the
# averaged prototype is markedly more stable across the odd framing in salvage.
PROMPTS: dict[str, list[str]] = {
    "photo": [
        "a personal photograph taken with a camera",
        "a candid snapshot of people",
        "a holiday photo of a place",
        "a family photograph",
        "an amateur photo taken indoors",
    ],
    "webcam": [
        "a surveillance camera still with a date and time stamp printed on it",
        "a webcam capture with a timestamp overlay in the corner",
        "a low quality security camera frame of a room",
        "a grainy CCTV image with text overlay",
    ],
    "screenshot": [
        "a screenshot of a computer application window",
        "a screenshot of a website in a web browser",
        "a screenshot of a software user interface with menus and buttons",
        "a screenshot of a desktop operating system",
    ],
    "web-graphic": [
        "a promotional advertisement banner",
        "a movie poster or product box art",
        "a meme image with caption text",
        "a stock photograph used on a website",
        "a logo or icon on a plain background",
        "an image that is mostly written text",
    ],
    "document": [
        "a scanned page of a printed document",
        "a photograph of a page of text",
        "a scanned receipt or form",
    ],
}

CLASSES = tuple(PROMPTS)

#: Sub-types within the `document` class. Kept separate from PROMPTS because they
#: only make sense once something is already known to be a document.
#:
#: `identity` is first deliberately: this library's document folder holds
#: passports and ID cards, and the operator should be able to see — and control —
#: exactly where those end up rather than discovering them in a shared folder.
DOC_PROMPTS: dict[str, list[str]] = {
    "identity": [
        "a passport photo page",
        "a national identity card",
        "a driving licence card",
        "an official identity document with a photograph",
    ],
    "receipt": [
        "a paper shop receipt",
        "a restaurant bill",
        "an invoice with amounts and totals",
        "a bank statement",
    ],
    "ticket": [
        "a boarding pass",
        "a travel ticket or reservation",
        "an event ticket with a barcode",
    ],
    "handwritten": [
        "a page of handwritten notes",
        "a handwritten letter",
        "a sticky note with handwriting",
        "a whiteboard covered in handwriting",
    ],
    "text-screenshot": [
        "a screenshot of a social media post that is mostly text",
        "a screenshot of an article or web page of text",
        "a screenshot of a chat conversation",
    ],
    "printed-page": [
        "a scanned page of a printed document",
        "a printed form or contract",
        "a page from a book or magazine",
    ],
}

DOC_TYPES = tuple(DOC_PROMPTS)

#: Sub-types within the `screenshot` class — what the capture is *of*.
#:
#: The screenshot view used to split by device, which answered a question nobody
#: asks: 11,469 of 15,130 came from one iPhone, so the biggest folder was simply
#: "my phone" and nothing was findable inside it. What a person looks for is the
#: kind of thing captured — a conversation, a ticket, a map — exactly as the
#: document view splits by kind rather than by scanner.
SHOT_PROMPTS: dict[str, list[str]] = {
    # `chat` and `social` are the pair that must be told apart deliberately. A
    # comment thread under a post is a conversation AND a social feed, and with
    # only "speech bubbles" to go on it landed in `chat`: the weakest tile there
    # was a column of TikTok replies. The prompts now say private on one side and
    # public on the other, and the comment section is named explicitly.
    "chat": [
        "a screenshot of a private messaging app conversation with speech bubbles",
        "a screenshot of a WhatsApp or Telegram chat between two people",
        "a screenshot of a text message thread on a phone",
        "a screenshot of a chat contact list",
    ],
    "social": [
        "a screenshot of a social media feed post",
        "a screenshot of an Instagram profile page",
        "a screenshot of a tweet",
        "a screenshot of the comment section underneath a post",
        "a screenshot of a short video app feed with likes and comments",
    ],
    "web-page": [
        "a screenshot of a web page in a browser",
        "a screenshot of a news article on a website",
        "a screenshot of a search results page",
    ],
    "maps": [
        "a screenshot of a street map",
        "a screenshot of a navigation route with directions",
        "a screenshot of a satellite map view",
    ],
    "media": [
        "a screenshot of a video playing with player controls",
        "a screenshot of a YouTube video page",
        "a screenshot of a music player with a track list",
        "a screenshot of a film or television scene",
        # Shazam's listening screen is a big pulsing circle and kept being read
        # as an activity ring. Naming it puts it where it belongs instead.
        "a screenshot of a music recognition app listening",
    ],
    "shopping": [
        "a screenshot of an online shop product page with a price",
        "a screenshot of a shopping cart or order confirmation",
        "a screenshot of a marketplace listing",
    ],
    "finance": [
        "a screenshot of a banking app showing a balance",
        "a screenshot of a payment confirmation",
        "a screenshot of a cryptocurrency price chart",
        "a screenshot of a financial transaction list",
    ],
    # These three name their SUBJECT, never their shape, and that is deliberate.
    # First attempt described what the capture looks like — "activity rings", "a
    # sleep or heart rate chart", "a to-do list with checkboxes" — and every
    # circular gauge and every vertical list of rows matched. `health` filled
    # with Shazam's pulsing circle, a broadband speed-test dial and crypto price
    # charts; `notes` filled with weather forecasts, which are exactly a column
    # of rows with icons and numbers. Roughly one tile in five was right.
    "health": [
        "a screenshot of a fitness tracking app showing steps walked and calories burned",
        "a screenshot of a workout summary with exercises and repetitions",
        "a screenshot of a sleep tracking app showing hours slept",
        "a screenshot of a medical or health record",
    ],
    "notes": [
        "a screenshot of a note written in a notes app",
        "a screenshot of a shopping list or a to-do list someone wrote",
        "a screenshot of a reminder or a calendar event",
    ],
    # The library's real download shape is a package manager, so it is named.
    "downloads": [
        "a screenshot of a package manager listing installable packages",
        "a screenshot of a download queue with progress bars",
        "a screenshot of a file manager showing folders and file sizes",
        "a screenshot of a torrent or transfer client",
    ],
    # Not requested, but required: weather forecasts are a column of rows with
    # icons and numbers, so without a home of their own they take over `notes`.
    # Seven of the weakest twenty-four `notes` tiles were the same weather app.
    "weather": [
        "a screenshot of a weather forecast with temperatures for each day",
        "a screenshot of a weather app showing the current temperature",
        "a screenshot of a rain radar map",
    ],
    "email": [
        "a screenshot of an email inbox with a list of messages",
        "a screenshot of an open email with a subject line and signature",
        "a screenshot of an email being composed",
    ],
    # NOT a type: `ai-chat` was tried and measured a net loss.
    #
    # `email` sits at ~65% and AI assistant screens are part of what is wrong
    # with it, so a type for them looked obvious. It is not: a conversation with
    # a chatbot and a conversation with a person are the same picture — speech
    # bubbles above a text field — and SigLIP has no way to tell them apart.
    # Measured over two prompt sets, the tighter one still reached only ~38%
    # precision, drained **123 captures out of `chat`** plus 47 from `social`
    # and 38 from `text`, and pushed `other` UP from 3,787 to 3,934. It made
    # every number worse in exchange for a folder that is mostly wrong.
    #
    # A type is only worth adding when its subject is visually distinct from the
    # ones already there. `weather` was; this is not.
    "game": [
        "a screenshot of a video game",
        "a screenshot of a mobile game with a score",
        "a screenshot of 3d game graphics",
    ],
    "code-terminal": [
        "a screenshot of source code in an editor",
        "a screenshot of a terminal window with command output",
        "a screenshot of a code diff",
    ],
    "app-ui": [
        "a screenshot of a settings menu",
        "a screenshot of a phone home screen with a grid of app icons",
        "a screenshot of a phone lock screen with notifications",
        "a screenshot of a phone lock screen showing the time over a wallpaper",
        "a screenshot of an alarm clock with a list of alarm times",
        "a screenshot of a dashboard with charts",
        "a screenshot of a form with input fields",
        "a screenshot of a list of installed applications",
    ],
    "photo-capture": [
        "a screenshot of a photograph shown full screen",
        "a saved picture of a person with no interface around it",
        "a screenshot of a photo gallery showing one image",
    ],
    "text": [
        "a screenshot that is mostly written text on a plain background",
        "a screenshot of a note or document of text",
    ],
}

SHOT_TYPES = tuple(SHOT_PROMPTS)

#: SigLIP is trained with a learned temperature in this region; measured here it
#: gives a mean top-class probability of 0.80 and calls 8% of files ambiguous —
#: a believable "I don't know" rate for a library this messy.
SOFTMAX_T = 0.01


def confident(sims, *, temperature: float = SOFTMAX_T, bar: float = 0.0):
    """Argmax of ``sims`` against its runner-up → ``(index, p)``, ``(None, p)`` below bar.

    A raw SigLIP similarity difference is not a confidence: the whole spread
    between the best and worst class here is about 0.08, so an absolute margin of
    0.01 — which looks small — rejected **24.5%** of the library as ambiguous and
    parked 3,131 files carrying camera EXIF in `unsorted`. Reading the scores
    through the model's own temperature fixes that.

    **The probability is taken between the top two only, and that matters.** A
    softmax over the whole candidate set is not comparable between taxonomies of
    different sizes, and silently tightens whenever one grows: adding four
    screenshot types claimed 382 captures out of `other` and pushed **1,253**
    previously-labelled ones into it, spread across every existing type, purely
    because four more candidates dilute every winner's share. Measured, `other`
    went *up* — 35.7% to 41.4% — from adding types meant to shrink it.

    Restricting the decision to the best two removes that: the extra candidates
    cancel in the normaliser. Across 12 vs 16 screenshot types the share below a
    0.70 bar moves 43.5% -> 44.6%, where the full softmax moved 35.7% -> 41.4%.
    It is also the better question. "Is this a photo or a web graphic?" is not
    informed by how certainly it is not a map — only the runner-up competes.
    """
    import numpy as np  # noqa: PLC0415

    v = np.asarray(sims, dtype="float64")
    i = int(np.argmax(v))
    if v.size < 2:
        return (i, 1.0) if 1.0 >= bar else (None, 1.0)
    second = float(np.partition(v, -2)[-2])
    p = 1.0 / (1.0 + np.exp(-(float(v[i]) - second) / temperature))
    return (i, p) if p >= bar else (None, p)


def _cache_path(model_id: str, prompts: dict | None = None) -> Path:
    import os  # noqa: PLC0415

    key = hashlib.sha1(  # noqa: S324 - cache key, not a security boundary
        (model_id + json.dumps(prompts or PROMPTS, sort_keys=True)).encode()
    ).hexdigest()[:16]
    d = Path(os.environ.get("NAVIG_HOME", Path.home() / ".navig")) / "cache" / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"class-prototypes-{key}.npy"


def _build(prompts: dict[str, list[str]], model: str | None, pretrained: str | None):
    """(labels, unit prototype matrix) for any prompt set, cached on disk."""
    import numpy as np  # noqa: PLC0415

    from . import embed as E  # noqa: PLC0415

    mid = E.model_id(model or E.DEFAULT_MODEL, pretrained or E.DEFAULT_PRETRAINED)
    cache = _cache_path(mid, prompts)
    labels = list(prompts)
    if cache.exists():
        return labels, np.load(cache)

    mats = []
    for label in labels:
        vecs = E.embed_text(prompts[label], model=model or E.DEFAULT_MODEL,
                            pretrained=pretrained or E.DEFAULT_PRETRAINED)
        v = vecs.mean(axis=0)
        mats.append(v / np.linalg.norm(v))
    mat = np.stack(mats).astype("float32")
    np.save(cache, mat)
    return labels, mat


def doc_prototypes(*, model: str | None = None, pretrained: str | None = None):
    """Prototypes for the document sub-types, cached like the main classes."""
    return _build(DOC_PROMPTS, model, pretrained)


def shot_prototypes(*, model: str | None = None, pretrained: str | None = None):
    """Prototypes for the screenshot sub-types."""
    return _build(SHOT_PROMPTS, model, pretrained)


def prototypes(*, model: str | None = None, pretrained: str | None = None):
    """Return ``(classes, matrix)`` of unit class vectors, cached on disk.

    Cached because building them needs the text tower — and therefore
    ``transformers``. After the first run, classifying a whole library needs
    neither.
    """
    return _build(PROMPTS, model, pretrained)


def score(image_vectors, *, model: str | None = None, pretrained: str | None = None):
    """(N, dim) unit image vectors → ``(classes, (N, n_classes) similarity)``."""
    import numpy as np  # noqa: PLC0415

    classes, protos = prototypes(model=model, pretrained=pretrained)
    if getattr(image_vectors, "size", 0) == 0:
        return classes, np.zeros((0, len(classes)), dtype="float32")
    return classes, np.asarray(image_vectors, dtype="float32") @ protos.T


def best(image_vectors, **kw):
    """→ list of ``(class, score)``, the single best class per image."""
    import numpy as np  # noqa: PLC0415

    classes, sims = score(image_vectors, **kw)
    idx = np.argmax(sims, axis=1)
    return [(classes[int(i)], float(sims[r, i])) for r, i in enumerate(idx)]

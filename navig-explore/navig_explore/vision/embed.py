"""SigLIP 2 image/text embeddings — the engine behind "photos of a red car on a beach".

SigLIP 2 (Apache-2.0, Google, 2025) beats OpenCLIP at every comparable scale on
zero-shot retrieval and covers 109 languages, which matters here: this library's
folder names are French and Russian ("14 juillet 2009", "Берлин 2014"), so
queries will be too.

Inference runs on **torch/CUDA, not ONNX Runtime**. The installed onnxruntime
exposes no CUDA provider, and an ORT built against the wrong cuDNN major silently
falls back to CPU at ~40x the cost. torch's bundled CUDA runtime has no such
ambiguity, so this module asserts the device it was asked for and fails loudly.
"""
from __future__ import annotations

import threading
from typing import Sequence

DEFAULT_MODEL = "ViT-B-16-SigLIP2"
DEFAULT_PRETRAINED = "webli"

_lock = threading.Lock()
_cache: dict[tuple, tuple] = {}


def model_id(model: str = DEFAULT_MODEL, pretrained: str = DEFAULT_PRETRAINED) -> str:
    """Stable identifier stored beside every vector, so a model swap is detectable."""
    return f"{model}:{pretrained}"


def resolve_device(device: str | None = None) -> str:
    import torch  # noqa: PLC0415

    if device and device != "auto":
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA requested but torch reports no CUDA device. "
                "Install a CUDA build of torch or pass --device cpu."
            )
        return device
    return "cuda" if torch.cuda.is_available() else "cpu"


def load(model: str = DEFAULT_MODEL, pretrained: str = DEFAULT_PRETRAINED,
         device: str | None = None):
    """Return ``(model, preprocess, device)``, cached per process.

    The tokenizer is deliberately *not* built here. SigLIP's tokenizer is a
    HuggingFace one, so touching it drags in ``transformers`` — a dependency the
    indexing path (the multi-hour one) never needs. Only `embed_text` pays for it.
    """
    import open_clip  # noqa: PLC0415

    dev = resolve_device(device)
    key = (model, pretrained, dev)
    with _lock:
        if key in _cache:
            return _cache[key]
        net, _, preprocess = open_clip.create_model_and_transforms(
            model, pretrained=pretrained, device=dev)
        net.eval()
        if dev.startswith("cuda"):
            net = net.half()
        _cache[key] = (net, preprocess, dev)
        return _cache[key]


def get_tokenizer(model: str = DEFAULT_MODEL):
    """Build the text tokenizer, with an actionable error if `transformers` is absent."""
    import open_clip  # noqa: PLC0415

    try:
        return open_clip.get_tokenizer(model)
    except ModuleNotFoundError as exc:  # transformers is only needed for text queries
        raise RuntimeError(
            f"Text search needs the `transformers` package ({model} uses a HuggingFace "
            f"tokenizer). Install it with:  py -3.13 -m pip install transformers"
        ) from exc


def embed_images(images: Sequence, *, model: str = DEFAULT_MODEL,
                 pretrained: str = DEFAULT_PRETRAINED, device: str | None = None):
    """Embed a batch of PIL images → (N, dim) float32 unit vectors."""
    import torch  # noqa: PLC0415

    if not images:
        import numpy as np  # noqa: PLC0415
        return np.zeros((0, 0), dtype="float32")

    net, preprocess, dev = load(model, pretrained, device)
    batch = torch.stack([preprocess(im) for im in images]).to(dev)
    if dev.startswith("cuda"):
        batch = batch.half()
    with torch.no_grad():
        feats = net.encode_image(batch)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.float().cpu().numpy()


def embed_text(queries: Sequence[str], *, model: str = DEFAULT_MODEL,
               pretrained: str = DEFAULT_PRETRAINED, device: str | None = None):
    """Embed text queries → (N, dim) float32 unit vectors.

    Defaults to CPU: one query is ~5 ms there, and keeping the text tower off the
    GPU means `photos search` never waits for a CUDA context or competes with an
    indexing run for VRAM.
    """
    import torch  # noqa: PLC0415

    net, _, dev = load(model, pretrained, device or "cpu")
    tokenizer = get_tokenizer(model)
    toks = tokenizer(list(queries)).to(dev)
    with torch.no_grad():
        feats = net.encode_text(toks)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.float().cpu().numpy()

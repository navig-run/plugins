"""Export / open / import vault SVGs — the one implementation both CLIs call.

``navig vault`` (core, Typer) and ``nv`` (this package, argparse) are thin fronts over the
functions here, so the two cannot drift into different behaviour.

Rules every front inherits:
  * nothing is ever overwritten — files are created exclusively;
  * a file name never carries a label (``vault-<random>.svg``), because the name travels
    in plaintext through every mailbox and chat the file passes;
  * every reference must resolve before anything is written, so a typo cannot produce a
    half export;
  * an item that will not decrypt on this machine is reported, never silently left out.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import svg, transfer

OPENER_NAME = "open-vault-svg.html"


class ExportError(Exception):
    pass


@dataclass
class WrittenFile:
    path: Path
    passphrase: str
    labels: list[str]
    generated: bool  # True when navig made the passphrase (it must be shown, once)


@dataclass
class ExportReport:
    files: list[WrittenFile] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    opener: Path | None = None


def opener_html() -> str:
    return resources.files("navig_vault").joinpath("opener.html").read_text(encoding="utf-8")


def _create(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except FileExistsError:
        raise ExportError(f"{path} already exists — nothing was overwritten") from None
    return path


def _random_path(folder: Path) -> Path:
    for _ in range(20):
        p = folder / f"vault-{secrets.token_hex(4)}.svg"
        if not p.exists():
            return p
    raise ExportError(f"could not find a free file name in {folder}")  # pragma: no cover


def save_file(folder: Path, name: str, data: bytes) -> Path:
    """Write an embedded file into *folder* under its own name — safely.

    The name comes from inside the SVG, i.e. from whoever made it, so it is reduced to a
    bare file name (no folders, no ``..``) and never replaces an existing file.
    """
    base = Path(name.replace("\\", "/")).name.strip().lstrip(".") or "embedded-file"
    stem, suffix = Path(base).stem, Path(base).suffix
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(1000):
        p = folder / (base if n == 0 else f"{stem} ({n}){suffix}")
        try:
            with open(p, "xb") as fh:
                fh.write(data)
            return p
        except FileExistsError:
            continue
    raise ExportError(f"could not find a free file name for {base} in {folder}")  # pragma: no cover


def write_opener(path: Path) -> Path:
    return _create(path, opener_html())


def export_svg(vault, refs: list[str] | None, *, folder: Path, out: Path | None = None,
               each: bool = False, passphrase: str | None = None, cover: str | None = None,
               with_opener: bool = False) -> ExportReport:
    """Seal the items *refs* name (``None`` = the whole vault) into SVG file(s).

    *passphrase* ``None`` → generated (one per file with *each*). A chosen passphrase must
    pass :func:`svg.check_passphrase`. *out* is the file to write (single export only);
    otherwise files get random names in *folder*.
    """
    if each and out is not None:
        raise ExportError("--each writes one file per item into a folder; give the folder, not -o")
    if out is not None and out.exists():
        raise ExportError(f"{out} already exists — nothing was overwritten")
    sealed = seal_texts(vault, refs, each=each, passphrase=passphrase, cover=cover)
    report = ExportReport(unreadable=sealed.unreadable)
    targets = [out] if out is not None else []
    opener_path = folder / OPENER_NAME if with_opener else None
    if opener_path is not None and opener_path.exists():
        opener_path = None  # one copy is enough; an existing opener is not overwritten
    for f in sealed.files:
        path = targets.pop() if targets else _random_path(folder)
        _create(path, f.text)
        report.files.append(WrittenFile(path, f.passphrase, f.labels, f.generated))
    if opener_path is not None:
        report.opener = write_opener(opener_path)
    return report


@dataclass
class SealedText:
    name: str  # vault-<random>.svg — never derived from a label
    text: str
    passphrase: str
    labels: list[str]
    generated: bool


@dataclass
class SealedExport:
    files: list[SealedText] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def seal_texts(vault, refs: list[str] | None, *, each: bool = False,
               passphrase: str | None = None, cover: str | None = None) -> SealedExport:
    """The same export, held in memory — for a caller that delivers the file itself.

    The desktop app gets the SVG text over the local deck API and saves it where the user
    chooses, so nothing is written on the daemon's side. Same rules as :func:`export_svg`.
    """
    if passphrase is not None:
        svg.check_passphrase(passphrase)
    items, missing = transfer.select_items(vault, refs)
    if missing:
        raise ExportError("nothing in the vault matches: " + ", ".join(missing))
    exported = transfer.export_entries(vault, items)
    if not exported.entries:
        raise ExportError("nothing to export" + (
            f" — {len(exported.unreadable)} item(s) cannot be decrypted on this machine"
            if exported.unreadable else " — the vault is empty"))
    out = SealedExport(unreadable=exported.unreadable)
    for group in ([[e] for e in exported.entries] if each else [exported.entries]):
        phrase = passphrase or svg.generate_passphrase()
        out.files.append(SealedText(
            name=f"vault-{secrets.token_hex(4)}.svg",
            text=svg.seal(group, phrase, cover=cover),
            passphrase=phrase,
            labels=[e["label"] for e in group],
            generated=passphrase is None,
        ))
    return out


def read_svg(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise svg.VaultSvgError(f"{path} is not a text SVG") from None

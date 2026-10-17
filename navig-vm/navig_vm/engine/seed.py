"""First-boot configuration: a cloud-init NoCloud seed, as a small ISO labelled CIDATA.

cloud-init reads ``user-data`` and ``meta-data`` from any attached filesystem labelled
``cidata``. That needs nothing inside the guest — unlike VMware's guestinfo channel, which
needs VMware Tools there first. The names must arrive exactly as written, so the ISO
carries Rock Ridge and Joliet names, not just 8.3 ones.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import string
from importlib import resources
from pathlib import Path

_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# The byte order of SHA-crypt's final encoding (Drepper, "Unix crypt using SHA-256 and
# SHA-512", section on the SHA-512 output).
_SHA512_ORDER = (
    (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26),
    (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32),
    (12, 33, 54), (34, 55, 13), (56, 14, 35), (15, 36, 57), (37, 58, 16), (59, 17, 38),
    (18, 39, 60), (40, 61, 19), (62, 20, 41),
)


def _b64_from_24bit(b2: int, b1: int, b0: int, n: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_ITOA64[w & 0x3F])
        w >>= 6
    return "".join(out)


def sha512_crypt(password: str, salt: str | None = None, rounds: int = 5000) -> str:
    """A ``$6$`` password hash (glibc's SHA-512 crypt), for ``/etc/shadow``.

    Python 3.13 removed ``crypt``, and a plaintext password in the seed would sit on the
    VM's attached seed disk for its whole life. Verified against the published test vector.
    """
    pw = password.encode("utf-8")
    salt = (salt if salt is not None else "".join(secrets.choice(_ITOA64) for _ in range(16)))[:16]
    s = salt.encode("ascii")

    b = hashlib.sha512(pw + s + pw).digest()
    a = hashlib.sha512(pw + s)
    for _ in range(len(pw) // 64):
        a.update(b)
    a.update(b[: len(pw) % 64])
    i = len(pw)
    while i:
        a.update(b if i & 1 else pw)
        i >>= 1
    c = a.digest()

    dp = hashlib.sha512(pw * len(pw)).digest()
    p = (dp * (len(pw) // 64 + 1))[: len(pw)]
    ds = hashlib.sha512(s * (16 + c[0])).digest()
    sb = (ds * (len(s) // 64 + 1))[: len(s)]

    for r in range(rounds):
        h = hashlib.sha512()
        h.update(p if r & 1 else c)
        if r % 3:
            h.update(sb)
        if r % 7:
            h.update(p)
        h.update(c if r & 1 else p)
        c = h.digest()

    encoded = "".join(_b64_from_24bit(c[x], c[y], c[z], 4) for x, y, z in _SHA512_ORDER)
    encoded += _b64_from_24bit(0, 0, c[63], 2)
    rounds_part = "" if rounds == 5000 else f"rounds={rounds}$"
    return f"$6${rounds_part}{salt}${encoded}"


def recipe_names() -> list[str]:
    root = resources.files("navig_vm") / "recipes"
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def render_recipe(recipe: str, values: dict[str, str]) -> str:
    """The recipe's ``user-data`` with ``${name}`` placeholders filled. Every placeholder
    must be supplied: a half-filled cloud-config fails at boot, far from the cause."""
    if recipe not in recipe_names():
        raise ValueError(f"unknown recipe {recipe!r} (have: {', '.join(recipe_names())})")
    text = (resources.files("navig_vm") / "recipes" / recipe / "user-data").read_text(encoding="utf-8")
    try:
        return string.Template(text).substitute(values)
    except KeyError as exc:
        raise ValueError(f"recipe {recipe!r} needs a value for {exc.args[0]}") from exc


def meta_data(instance_id: str, hostname: str) -> str:
    return f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n"


def write_seed_iso(path: Path, user_data: str, meta: str) -> Path:
    """Write the NoCloud seed ISO (label CIDATA) holding ``user-data`` and ``meta-data``."""
    import pycdlib  # noqa: PLC0415 — only `create` needs it

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="CIDATA")
    for name, body in (("user-data", user_data), ("meta-data", meta)):
        data = body.encode("utf-8")
        iso.add_fp(
            io.BytesIO(data),
            len(data),
            f"/{name.upper().replace('-', '_')}.;1",
            rr_name=name,
            joliet_path=f"/{name}",
        )
    tmp = path.with_suffix(path.suffix + ".part")
    iso.write(str(tmp))
    iso.close()
    tmp.replace(path)
    return path

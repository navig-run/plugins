#!/usr/bin/env python3
"""Recover a navig cabinet backup WITHOUT navig: turn a .ncab file into a plain .tar.

    pip install cryptography
    python recover_backup.py my-backup.ncab recovered.tar      # asks for the passphrase
    NCAB_PASSPHRASE=... python recover_backup.py my-backup.ncab recovered.tar   # scripted
    tar -xf recovered.tar          # manifest.json + files/<id>/<original name>

Deliberately standalone — standard library + ``cryptography`` only — so the documents
outlive the tool that stored them. Keep a copy of this file next to your backups.
"""

import base64
import getpass
import hashlib
import json
import os
import struct
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def recover(bundle_path, out_path, passphrase):
    with open(bundle_path, "rb") as src, open(out_path, "wb") as out:
        if src.read(8) != b"NCABBAK1":
            raise SystemExit("not a navig cabinet backup")
        (hlen,) = struct.unpack(">I", src.read(4))
        header = src.read(hlen)
        kdf = json.loads(header)["kdf"]
        key = Scrypt(salt=base64.b64decode(kdf["salt"]), length=32, n=kdf["n"], r=kdf["r"],
                     p=kdf["p"]).derive(passphrase.encode("utf-8"))
        context = b"navig-cabinet/backup/" + hashlib.sha256(header).digest()
        stream_header = src.read(12)                      # b"NCABSTR1" + u32 chunk size
        if stream_header[:8] != b"NCABSTR1":
            raise SystemExit("backup stream header is damaged")
        aead, index = AESGCM(key), 0
        while True:
            raw = src.read(4)
            if len(raw) < 4:
                raise SystemExit("backup is truncated")
            (ct_len,) = struct.unpack(">I", raw)
            nonce, ct = src.read(12), src.read(ct_len)
            for last in (0, 1):
                aad = stream_header + context + struct.pack(">QB", index, last)
                try:
                    out.write(aead.decrypt(nonce, ct, aad))
                    break
                except Exception:
                    if last:
                        raise SystemExit("wrong passphrase, or the backup is damaged")
            index += 1
            if last:
                return


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python recover_backup.py <backup.ncab> <out.tar>")
    # getpass reads the console directly on Windows and ignores piped input, so a
    # scripted recovery can pass the passphrase in NCAB_PASSPHRASE instead.
    passphrase = os.environ.get("NCAB_PASSPHRASE") or getpass.getpass("Backup passphrase: ")
    recover(sys.argv[1], sys.argv[2], passphrase)
    print(f"Recovered -> {sys.argv[2]} (open it with any archive tool)")

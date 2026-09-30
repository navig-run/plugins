"""``navig-vault`` / ``nv`` -- the standalone command line.

Built on argparse, deliberately. This package is imported as a LIBRARY by anything that
needs to read a secret, so it must stay light: adding typer/click/rich as hard dependencies
would push a CLI framework into every consumer's environment to serve a binary most of them
never run. The engine is the product; the CLI is a thin front for it.

Security shape, which is most of the design here
------------------------------------------------
* **A secret never comes from argv.** Command-line arguments are visible in shell history
  and to every other process on the machine via ``ps``/Task Manager. So ``add`` reads the
  value from an env var, a file, or an interactive prompt that does not echo -- never a
  ``--value`` flag. There is deliberately no way to pass one.
* **``get`` writes the raw secret to stdout and nothing else**, so ``$(nv get openai)`` and
  ``nv get openai | docker secret create`` work. Diagnostics go to stderr, which is why they
  cannot corrupt a piped value.
* **Exit codes are load-bearing**: 0 only when the thing asked for actually happened, 1 for
  a real failure, 2 for usage, 3 for "not found". A caller chaining on ``&&`` must not
  proceed as though a secret was stored or read when it was not.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import __version__

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _vault(args: argparse.Namespace):
    """Open the vault, honouring --vault-dir.

    Imported lazily so that ``--help`` and ``--version`` do not pay for cryptography.
    """
    from .core import get_vault

    return get_vault(Path(args.vault_dir) if args.vault_dir else None)


# ── secret input: never from argv ────────────────────────────────────────────


def _read_secret(args: argparse.Namespace, prompt: str) -> str | None:
    """Obtain a secret value from env, a file, stdin, or an interactive prompt.

    Order is deliberate: the non-interactive sources come first so scripting works without
    a TTY, and the prompt is the fallback rather than the default.
    """
    if args.from_env:
        val = os.environ.get(args.from_env)
        if not val:
            _err(f"environment variable {args.from_env!r} is empty or unset")
            return None
        return val
    if args.from_file:
        p = Path(args.from_file)
        try:
            # .strip() only trailing newline: a secret may legitimately contain spaces.
            return p.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            _err(f"cannot read {p}: {exc}")
            return None
    if args.stdin:
        data = sys.stdin.read().rstrip("\r\n")
        if not data:
            _err("no value on stdin")
            return None
        return data
    if not sys.stdin.isatty():
        _err(
            "no value supplied and stdin is not a terminal.\n"
            "Use --from-env VAR, --from-file PATH, or --stdin."
        )
        return None
    try:
        value = getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        _err("\naborted")
        return None
    if not value:
        _err("empty value; nothing stored")
        return None
    return value


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_add(args: argparse.Namespace) -> int:
    from .provider import get_provider

    provider = args.provider
    spec = get_provider(provider)
    field = args.field or (spec.get("key_field") if isinstance(spec, dict) else None) or "api_key"

    value = _read_secret(args, f"{provider} {field}: ")
    if value is None:
        return EXIT_FAIL

    v = _vault(args)
    cred_id = v.add(
        provider=provider,
        credential_type=args.type,
        data={field: value},
        label=args.label,
    )
    if not cred_id:
        _err(f"failed to store credential for {provider!r}")
        return EXIT_FAIL
    print(f"stored {provider} ({field})", file=sys.stderr)
    return EXIT_OK


def cmd_get(args: argparse.Namespace) -> int:
    v = _vault(args)
    if args.field:
        cred = v.get(args.provider, caller="navig-vault-cli")
        value = None
        if cred is not None:
            data = getattr(cred, "data", None) or {}
            value = data.get(args.field)
    else:
        value = v.get_api_key(args.provider, caller="navig-vault-cli")

    if value is None:
        _err(f"no credential found for {args.provider!r}")
        return EXIT_NOT_FOUND
    # stdout carries the value and nothing else, so $( ) and pipes stay clean.
    sys.stdout.write(str(value))
    if sys.stdout.isatty():
        sys.stdout.write("\n")
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    v = _vault(args)
    items = v.list(provider=args.provider)
    if args.json:
        print(json.dumps([_item_dict(i) for i in items], indent=2, sort_keys=True))
        return EXIT_OK
    if not items:
        print("vault is empty" if not args.provider else f"nothing stored for {args.provider!r}",
              file=sys.stderr)
        return EXIT_OK
    width = max(len(str(getattr(i, "provider", "") or "")) for i in items)
    for i in items:
        prov = str(getattr(i, "provider", "") or "")
        label = str(getattr(i, "label", "") or "")
        kind = getattr(getattr(i, "kind", None), "value", getattr(i, "kind", "")) or ""
        print(f"{prov:<{width}}  {kind:<10}  {label}")
    return EXIT_OK


def _item_dict(item) -> dict:
    """A VaultItem as plain data, with no secret material in it."""
    kind = getattr(getattr(item, "kind", None), "value", getattr(item, "kind", None))
    return {
        "id": getattr(item, "id", None),
        "provider": getattr(item, "provider", None),
        "label": getattr(item, "label", None),
        "kind": kind,
        "created_at": str(getattr(item, "created_at", "") or "") or None,
        "updated_at": str(getattr(item, "updated_at", "") or "") or None,
    }


def cmd_delete(args: argparse.Namespace) -> int:
    v = _vault(args)
    if not args.yes:
        if not sys.stdin.isatty():
            _err("refusing to delete without --yes when stdin is not a terminal")
            return EXIT_USAGE
        reply = input(f"delete credential {args.target!r}? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            _err("aborted")
            return EXIT_FAIL
    if not v.delete(args.target):
        _err(f"no credential matched {args.target!r}")
        return EXIT_NOT_FOUND
    print(f"deleted {args.target}", file=sys.stderr)
    return EXIT_OK


def cmd_providers(args: argparse.Namespace) -> int:
    from .provider import PROVIDERS

    if args.json:
        print(json.dumps(sorted(PROVIDERS), indent=2))
        return EXIT_OK
    for name in sorted(PROVIDERS):
        spec = PROVIDERS[name]
        field = spec.get("key_field", "") if isinstance(spec, dict) else ""
        env = spec.get("env", "") if isinstance(spec, dict) else ""
        print(f"{name:<18}  {field:<16}  {env}")
    return EXIT_OK


def cmd_test(args: argparse.Namespace) -> int:
    try:
        from .validators import get_validator
    except ImportError:  # pragma: no cover - only without the extra
        _err("credential checks need the validators extra: pip install 'navig-vault[validators]'")
        return EXIT_FAIL

    v = _vault(args)
    cred = v.get(args.provider, caller="navig-vault-cli")
    if cred is None:
        _err(f"no credential found for {args.provider!r}")
        return EXIT_NOT_FOUND
    result = get_validator(args.provider).validate(cred)
    ok = bool(getattr(result, "success", False))
    print(f"{'ok' if ok else 'FAILED'}: {getattr(result, 'message', '')}",
          file=sys.stdout if ok else sys.stderr)
    return EXIT_OK if ok else EXIT_FAIL


def cmd_path(args: argparse.Namespace) -> int:
    """Where the vault actually is -- the first thing to check when two installs disagree."""
    from ._compat import navig_available, vault_dir

    d = Path(args.vault_dir) if args.vault_dir else vault_dir()
    print(d)
    _err(f"navig installed: {navig_available()}  ·  db exists: {(d / 'vault.db').exists()}")
    return EXIT_OK


# ── parser ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="navig-vault",
        description="Encrypted developer secrets manager. Shares one vault with navig.",
    )
    p.add_argument("-V", "--version", action="version", version=f"navig-vault {__version__}")
    p.add_argument("--vault-dir", metavar="DIR",
                   help="vault directory (default: navig's, honouring NAVIG_VAULT_DIR)")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def secret_sources(sp):
        # A --value flag is deliberately absent: argv is world-readable via ps.
        g = sp.add_argument_group("value source (never taken on the command line)")
        g.add_argument("--from-env", metavar="VAR", help="read the value from an env var")
        g.add_argument("--from-file", metavar="PATH", help="read the value from a file")
        g.add_argument("--stdin", action="store_true", help="read the value from stdin")

    a = sub.add_parser("add", help="store a credential")
    a.add_argument("provider")
    a.add_argument("--field", help="field name (default: the provider's key field)")
    a.add_argument("--type", default="api_key", help="credential type (default: api_key)")
    a.add_argument("--label", help="optional label")
    secret_sources(a)
    a.set_defaults(func=cmd_add)

    g = sub.add_parser("get", help="print a secret to stdout")
    g.add_argument("provider")
    g.add_argument("--field", help="a specific field instead of the api key")
    g.set_defaults(func=cmd_get)

    ls = sub.add_parser("list", help="list stored credentials (never prints secrets)")
    ls.add_argument("provider", nargs="?", help="only this provider")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    d = sub.add_parser("delete", help="remove a credential")
    d.add_argument("target", help="provider, label, or id")
    d.add_argument("-y", "--yes", action="store_true", help="do not prompt")
    d.set_defaults(func=cmd_delete)

    pr = sub.add_parser("providers", help="list known providers and their env vars")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_providers)

    t = sub.add_parser("test", help="check a stored credential against its provider")
    t.add_argument("provider")
    t.set_defaults(func=cmd_test)

    pa = sub.add_parser("path", help="print the vault directory in use")
    pa.set_defaults(func=cmd_path)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        _err("\naborted")
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001 - a CLI must not traceback at a user
        _err(f"error: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())

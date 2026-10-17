"""navig-media-core is the foundation: it never imports the plugins built on it (ADR-0002).

A library that lazily reaches into ``navig_generate`` is a hidden reverse dependency: it
"works" only when generate happens to be installed, and every refactor of generate can break
it with no declared edge to warn anyone. These tests pin the direction of the graph.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "navig_media_core"

# The media plugins that depend ON this package — importing any of them inverts the graph.
SIBLINGS = (
    "navig_generate", "navig_audio", "navig_video", "navig_text", "navig_pipeline", "navig_media",
)
# The one module allowed to: a deprecated identity alias of the service's new home.
ALIAS_EXEMPT = {"media/generation_service.py"}


def _sources() -> list[Path]:
    files = sorted(PKG.rglob("*.py"))
    # Scan floor: an empty or partial checkout must not pass by scanning nothing.
    assert len(files) >= 12, f"only {len(files)} modules found under {PKG}"
    return files


def _imported_roots(tree: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.lineno, node.module.split(".")[0]))
    return out


def test_never_imports_a_sibling_media_plugin() -> None:
    offenders = []
    for path in _sources():
        rel = path.relative_to(PKG).as_posix()
        if rel in ALIAS_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [f"{rel}:{line} imports {root}" for line, root in _imported_roots(tree)
                      if root in SIBLINGS]
    assert not offenders, "navig-media-core must not import plugins built on it:\n" + "\n".join(offenders)


def test_navig_is_only_reached_through_guarded_or_local_imports() -> None:
    """navig is optional: a module-level ``import navig…`` would break the standalone install."""
    offenders = []
    for path in _sources():
        rel = path.relative_to(PKG).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module level only; function bodies and try blocks are fine
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                continue
            for line, root in _imported_roots(node) if isinstance(node, (ast.Import, ast.ImportFrom)) else []:
                if root == "navig":
                    offenders.append(f"{rel}:{line}")
    assert not offenders, "unguarded module-level navig import(s): " + ", ".join(offenders)


def test_exempt_alias_is_still_only_an_alias() -> None:
    """The exemption must not grow into code: the alias file holds no function or class."""
    for rel in ALIAS_EXEMPT:
        tree = ast.parse((PKG / rel).read_text(encoding="utf-8"))
        defs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        assert defs == [], f"{rel} is exempt only as an alias, but defines {defs}"


def test_old_service_path_is_the_same_module_object() -> None:
    pytest.importorskip("navig_generate")
    import navig_generate.media.generation_service as owner
    import navig_media_core.media.generation_service as legacy

    assert legacy is owner

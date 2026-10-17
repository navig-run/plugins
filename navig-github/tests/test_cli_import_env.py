"""The GitHub command must be safe for NAVIG and Typer to introspect."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_importing_engine_cli_does_not_load_project_dotenv(tmp_path: Path) -> None:
    key = "NAVIG_GITHUB_IMPORT_SENTINEL"
    (tmp_path / ".env").write_text(f"{key}=leaked\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop(key, None)
    plugin_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugin_root), *(part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part)]
    )

    run = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; import navig_github.engine.cli; "
            f"print(os.environ.get('{key}', 'absent'))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert run.stdout.strip() == "absent"

"""Installed-game model shared by the library scanners + Steam unification."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class InstalledGame:
    store: str  # steam | epic | gog | amazon
    title: str
    app_id: str = ""
    install_dir: str = ""
    launch_exe: str = ""  # absolute path to the executable (target for a Steam shortcut)
    launch_options: str = ""

    @property
    def key(self) -> str:
        ident = self.app_id or self.launch_exe or self.title
        return f"{self.store}:{ident}".lower()

    @property
    def launchable(self) -> bool:
        """True when we resolved a real executable (required to add to Steam)."""
        return bool(self.launch_exe)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["launchable"] = self.launchable
        return d

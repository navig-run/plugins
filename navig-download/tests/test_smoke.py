"""Smoke tests for navig-download — the package imports and its surface exists."""

from __future__ import annotations


def test_engine_availability_helpers_return_bool():
    from navig_download.tiktok import downloader_available, ytdlp_available

    assert isinstance(downloader_available(), bool)
    assert isinstance(ytdlp_available(), bool)


def test_command_apps_exist():
    from navig_download.commands.download import download_app, tiktok_app, tt_app, tiktok_download

    # `download`/`dl` and `tiktok`/`tt` are the same Typer app (back-compat).
    assert download_app is tiktok_app
    assert tt_app is tiktok_app
    assert callable(tiktok_download)


def test_plugin_register_is_importable():
    from navig_download import plugin

    assert hasattr(plugin, "register")

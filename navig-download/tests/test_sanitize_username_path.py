"""sanitize_username must produce a filesystem-safe path segment.

Its result is joined onto output_dir and passed to os.makedirs / yt-dlp outtmpl,
so a username carrying a path separator or '..' must not escape the output dir
(a crafted/agent-supplied handle would otherwise create dirs and write files
outside it).
"""

from __future__ import annotations

import os

import pytest

from navig_download.downloader.main import sanitize_username


def test_strips_at_prefix_and_whitespace():
    assert sanitize_username("@alice") == "alice"
    assert sanitize_username("  @alice  ") == "alice"


def test_preserves_a_legit_handle():
    # Real TikTok handles are [A-Za-z0-9._] — untouched.
    assert sanitize_username("user.name_123") == "user.name_123"


def test_path_separators_are_neutralised():
    assert "/" not in sanitize_username("a/b/c")
    assert "\\" not in sanitize_username("a\\b\\c")
    assert sanitize_username("../../evil") == "evil"
    assert sanitize_username("/etc/passwd") == "etc_passwd"


def test_traversal_only_segment_raises():
    # "..", ".", "/" etc. reduce to empty → refused rather than becoming a blank dir.
    for bad in ("..", ".", "/", "\\", "...", "   /  "):
        with pytest.raises(ValueError):
            sanitize_username(bad)


def test_empty_raises():
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            sanitize_username(bad)


def test_result_can_never_escape_output_dir(tmp_path):
    out = os.path.abspath(str(tmp_path / "downloads"))
    for evil in ("../../../../etc", "..\\..\\windows", "/etc/passwd", "a/../../b", "@../x"):
        seg = sanitize_username(evil)
        joined = os.path.abspath(os.path.join(out, seg))
        # commonpath == out proves the join stayed inside the output dir.
        assert os.path.commonpath([out, joined]) == out, (evil, seg, joined)

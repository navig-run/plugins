"""The canonical social brand-label map (`navig_social.social.labels`) and its
delegation from the publisher registry.

Guards the single-source rule: brand casing lives ONLY in ``DISPLAY_LABELS``, the
publisher ``display_label`` reads it, and no publisher class re-declares ``label``.
This map feeds both the ``/studio/networks`` API (→ every webui surface) and the
``navig social`` CLI tables, so a regression here mis-brands the whole UI.
"""

from __future__ import annotations

from navig_social.social.labels import DISPLAY_LABELS, display_label
from navig_social.social.publishers import (
    DevToPublisher,
    LinkedInPublisher,
    RedditPublisher,
    YouTubePublisher,
)


class TestDisplayLabel:
    def test_overrides(self):
        assert display_label("linkedin") == "LinkedIn"
        assert display_label("youtube") == "YouTube"
        assert display_label("devto") == "Dev.to"

    def test_title_fallback(self):
        # Not in the map → a plain .title() is already correct.
        assert display_label("reddit") == "Reddit"
        assert display_label("twitter") == "Twitter"
        assert display_label("facebook") == "Facebook"

    def test_underscored_id(self):
        assert display_label("some_network") == "Some Network"

    def test_case_insensitive_key(self):
        assert display_label("LinkedIn") == "LinkedIn"
        assert display_label("YOUTUBE") == "YouTube"

    def test_empty_passthrough(self):
        # Callers rely on this to keep their own "(unattributed)" fallback.
        assert display_label("") == ""


class TestPublisherDelegation:
    def test_publishers_render_brand_casing(self):
        assert LinkedInPublisher().display_label == "LinkedIn"
        assert YouTubePublisher().display_label == "YouTube"
        assert DevToPublisher().display_label == "Dev.to"
        assert RedditPublisher().display_label == "Reddit"  # no override → fallback

    def test_no_publisher_redeclares_label(self):
        # Single-source guard: the 3 brands live in DISPLAY_LABELS, NOT on the class.
        for cls in (LinkedInPublisher, YouTubePublisher, DevToPublisher):
            assert "label" not in cls.__dict__, f"{cls.__name__} should not set `label`"

    def test_map_holds_the_overrides(self):
        assert DISPLAY_LABELS["linkedin"] == "LinkedIn"
        assert DISPLAY_LABELS["youtube"] == "YouTube"
        assert DISPLAY_LABELS["devto"] == "Dev.to"

"""Small helpers navig provides, with a standalone twin when navig is absent."""

from __future__ import annotations

import html


def escape_html(text: object) -> str:
    """Telegram-HTML escaping: navig's own when installed, the same ``&<>`` escape otherwise."""
    try:
        from navig.messaging.notify_operator import escape_html as _impl
    except ImportError:
        return html.escape(str(text), quote=False)
    return _impl(text)

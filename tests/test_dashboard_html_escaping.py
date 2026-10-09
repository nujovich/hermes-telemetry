"""Escaping contract for the standalone dashboard (dashboard/index.html).

Error messages can carry server-supplied text, so they must never be
interpolated raw into an innerHTML template: they go through escHtml(...)
or are assigned via textContent.
"""

import re
from pathlib import Path

INDEX_HTML = Path(__file__).parent.parent / "dashboard" / "index.html"


# A template interpolation of error text (`err`, `e.message`, `error.message`,
# ...) that is not wrapped in escHtml(...). Quotes are excluded from the scan
# so string literals such as statusBadge('error') do not match.
_RAW_ERROR_INTERPOLATION = re.compile(
    r"\$\{\s*(?!escHtml\()[^}'\"]*(?:\.message\b|\berr\b|\berror\b)"
)


def test_error_text_is_never_interpolated_raw():
    html = INDEX_HTML.read_text(encoding="utf-8")
    raw = [
        (lineno, line.strip())
        for lineno, line in enumerate(html.splitlines(), start=1)
        if _RAW_ERROR_INTERPOLATION.search(line)
    ]
    assert not raw, f"unescaped error text in dashboard/index.html: {raw}"

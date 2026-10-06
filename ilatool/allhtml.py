"""Editing the card list in ``texts/all.html``.

``all.html`` keeps its list of texts as a plain JavaScript array of object
literals, so this deliberately does not try to parse the page: it finds the
marker comment and splices in one more entry in the same style.  That keeps
the change reviewable in a diff and avoids reformatting the rest of the file.
"""

from __future__ import annotations

import re
ALL_HTML_MARKER = '// Add further text entries here'
ALL_HTML_CASE_RE = re.compile(
    r"""\{\s*
        title:\s*'(?P<title>(?:[^'\\]|\\.)*)'\s*,\s*
        subtitle:\s*'(?P<subtitle>(?:[^'\\]|\\.)*)'\s*,\s*
        desc:\s*'(?P<desc>(?:[^'\\]|\\.)*)'\s*,\s*
        href:\s*'(?P<href>(?:[^'\\]|\\.)*)'\s*
    \}""",
    re.VERBOSE | re.DOTALL,
)


def js_str_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace("'", "\\'").replace('\n', ' ')


def find_all_html_cases(all_html_text: str):
    """Returns a list of dicts (title/subtitle/desc/href) for every card
    currently in all.html's `cases` array."""
    return [m.groupdict() for m in ALL_HTML_CASE_RE.finditer(all_html_text)]


def add_case_to_all_html(all_html_text: str, title: str, subtitle: str, desc: str, href: str) -> str:
    """Returns updated all.html text with one more case object spliced in
    right after the marker comment, matching the existing entries' style."""
    if ALL_HTML_MARKER not in all_html_text:
        raise ValueError(
            f"couldn't find the marker comment ('{ALL_HTML_MARKER}') in all.html -- "
            "has the file's structure changed?"
        )
    entry = (
        "\n{\n"
        f"  title: '{js_str_escape(title)}',\n"
        f"  subtitle: '{js_str_escape(subtitle)}',\n"
        f"  desc: '{js_str_escape(desc)}',\n"
        f"  href: '{js_str_escape(href)}'\n"
        "},"
    )
    return all_html_text.replace(ALL_HTML_MARKER, ALL_HTML_MARKER + entry, 1)


def replace_case_in_all_html(all_html_text: str, href: str, title: str, subtitle: str, desc: str) -> str:
    """Replace an existing case object (matched by href) in-place, keeping
    its position in the array rather than appending a duplicate."""
    def repl(m):
        if m.group('href') != href:
            return m.group(0)
        return (
            "{\n"
            f"  title: '{js_str_escape(title)}',\n"
            f"  subtitle: '{js_str_escape(subtitle)}',\n"
            f"  desc: '{js_str_escape(desc)}',\n"
            f"  href: '{js_str_escape(href)}'\n"
            "}"
        )
    return ALL_HTML_CASE_RE.sub(repl, all_html_text, count=0)

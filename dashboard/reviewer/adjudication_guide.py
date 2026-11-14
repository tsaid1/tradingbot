"""The adjudication workspace's "Reviewer guide" page: the same text as docs/REVIEWER_GUIDE.md
(both from ``REVIEWER_GUIDE`` in ``runtime/adjudication_rubric.py``). Static: it reads no bundle,
no log and no file, and renders plain text only. Reached through the workspace's navigation,
which has already checked the server, the folder and the session (both access modes)."""

from __future__ import annotations

import streamlit as st

from tradingbot.runtime import adjudication_rubric as R


def blocks(text: str) -> list[tuple[str, str]]:
    """(kind, text): ``title``, ``heading`` or ``para`` (a bullet or a paragraph, joined)."""
    out: list[tuple[str, str]] = []
    para: list[str] = []

    def flush():
        if para:
            out.append(("para", " ".join(para)))
            para.clear()

    for line in text.splitlines():
        if line.startswith("# "):
            flush()
            out.append(("title", line[2:]))
        elif line.startswith("## "):
            flush()
            out.append(("heading", line[3:]))
        elif not line.strip():
            flush()
        elif line.startswith(("- ", "1.", "2.", "3.", "4.", "5.", "6.")):
            flush()
            para.append(line.strip())
        else:
            para.append(line.strip())
    flush()
    return out


for kind, body in blocks(R.REVIEWER_GUIDE):
    {"title": st.title, "heading": st.subheader, "para": st.text}[kind](body)

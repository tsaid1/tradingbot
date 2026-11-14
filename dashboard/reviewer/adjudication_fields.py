"""The adjudication workspace's "Field reference" page, rendered from the rubric's table
(``RUBRIC`` in ``runtime/adjudication_rubric.py``, the same source as docs/ADJUDICATION_RUBRIC.md):
every field, its values with their meanings, and which fields apply. Static and plain text."""

from __future__ import annotations

import streamlit as st

from tradingbot.runtime import adjudication_rubric as R

st.title("Field reference")
st.text("Answer states: \"\" unanswered; NOT_STATED the publisher did not state it; "
        "CANNOT_DETERMINE it probably exists but the evidence does not settle it (needs an "
        "ambiguity or a flag); NOT_APPLICABLE set by the form when a field does not apply.")
st.subheader("Which fields apply")
st.code(R.applicability_text(), language=None)
for code, title in R.GROUPS:
    st.header(f"{code}. {title}")
    for f in (x for x in R.RUBRIC if x.group == code):
        st.subheader(f"{f.question} [{f.name}]")
        st.text(f.tooltip)
        if f.applies:
            st.text(f"Applies: {f.applies}.")
        if f.set_by_form:
            st.text(f"Set by the form: {f.set_by_form}.")
        st.code("\n".join(f"{c}: {m}" for c, m in f.values), language=None)
        for e in f.examples:
            st.text(f"Invented example: {e}")
st.subheader("What Save refuses")
st.code("\n".join(f"{c}: {m}" for c, m in R.BLOCKING_CODES.items()), language=None)
st.subheader("Warnings (saving needs the tick \"I have checked these\")")
st.code("\n".join(f"{c}: {m}" for c, m in R.WARNING_CODES.items()), language=None)

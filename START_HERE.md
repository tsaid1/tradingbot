# Start here

1. Clone `https://github.com/tsaid1/tradingbot.git` on your Mac.
2. Copy the **contents** of this starter package into the root of that empty clone.
3. Follow `README.md` quickstart.
4. Run `pytest`, then `streamlit run dashboard/app.py`.
5. Commit and push to your own feature branch; open PR to `main`.
6. Assign the second agent only the research PR described in `AGENTS.md`.

**First demonstration**: Enter `BUY ABC @ 100 STOP 95 TARGET 110` with the default
synthetic quote. Check the MOCK order. Reuse the source ID and confirm `DUPLICATE`.
Create another source ID with `CLOSE ABC IDEA <first-source-id>` and confirm
that the fictional position is closed.

**Limits**: the code is not pushed automatically. This archive is prepared for
integration into your GitHub repository; no authenticated GitHub write access is
available from the current environment.

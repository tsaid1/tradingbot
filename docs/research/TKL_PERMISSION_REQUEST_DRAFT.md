# Draft: permission request to TKL for personal automated processing

Status: DRAFT for the product owner to adapt and send (spec decision D-07). Not sent.

Why: TKL's terms describe the `@TKLSubscribers` feed as a revocable "bonus perk" and bar storage or reproduction "except as permitted with prior written permission". Reading it through the official X API and acting on it automatically is not expressly prohibited, but written confirmation removes the main contractual risk identified in `X_PROTECTED_ACCOUNT_API_FEASIBILITY.md`.

---

Subject: Personal automated reading of the @TKLSubscribers feed

Hello,

I am an individual subscriber (handle: @<subscriber handle>, plan: <Individual/Professional>). I would like to confirm that the following personal use of the private feed is acceptable to you.

1. My own X account, which you have approved as a follower, reads @TKLSubscribers posts through X's official API under my own developer credentials. No scraping, no shared logins, no third-party access.
2. Software I run for myself reads each post once to decide whether to place a trade in my own brokerage account. The posts are not shown to anyone else, not republished, not stored beyond a short operational window, and not used to train any model.
3. I retain only post IDs, timestamps and hashes for my own audit trail, and delete post text and images after processing.

I will not redistribute any TKL content, and I understand the feed remains a subscriber benefit that you may withdraw. If any part of this is outside your terms, please tell me what you would permit, or whether you offer a structured subscriber feed for this purpose.

Thank you,
<name>

---

Checklist before sending: fill in the handle and plan; confirm the retention statement matches what the system will actually do (spec section 14.4); keep the reply with the project records as the license evidence (`license_tag` in the ingress contract).

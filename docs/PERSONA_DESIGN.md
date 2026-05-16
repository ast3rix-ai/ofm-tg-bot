# Persona design

**Status:** stub, populated in Phase 8 (initial structure may land earlier).

This document will house the persona definition for each operated account. A persona includes:

- Identity: name, age, location, backstory.
- Voice samples: 20+ real DMs in her voice across registers.
- Voice rules: vocabulary preferences, banned words, punctuation/capitalization habits, emoji usage.
- Offer catalog: PPV menu, prices, subscription tiers, custom request pricing.
- Hard rules: what she never agrees to, what she always responds to, escalation triggers.
- Few-shot exemplars by category: cold opener, price ask, objection handling, post-purchase, etc.

Persona files live in `personas/<account_handle>/` (created Phase 8).

Do not populate this file until samples and offer info are provided.

## MVP placeholder (Phase 5)

Phase 5 ships a minimal hardcoded persona at [`personas/default/persona.md`](../personas/default/persona.md) so the response generator has something to work with. It is intentionally thin — identity, voice rules, hard rules, and a strict length rule, no voice samples or offer catalog. The response generator reads it on first use and hot-reloads it when its mtime changes.

This is a stand-in. The real per-account persona — voice samples, offer catalog, few-shot exemplars — lands in Phase 8 and replaces it.

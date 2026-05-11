# Chat categories

Each chat has exactly one **category** (the funnel stage) and zero or more **flags** (orthogonal states). Categories evolve over time as the classifier re-runs. Flags can stack.

## Categories

### `cold`
**Definition:** Initial small talk, no buying signals. Customer engaging conversationally but hasn't expressed interest in content, pricing, or services.
**Entry signals:** Greetings, "how are you," general chitchat from a new contact.
**Exit signals:** Any mention of content, prices, body, what she does — moves to `warm`.

### `warm`
**Definition:** Engaged customer showing interest in her or her content. Not yet asking specific buying questions.
**Entry signals:** Compliments about her, asking about her day with intent, "what do you do," flirting that goes beyond greetings.
**Exit signals:** Explicit asks about content/menu/prices → `hot`.

### `hot`
**Definition:** Explicit buying signals. Asking about content, prices, menu, what's available.
**Entry signals:** "how much," "do you have ...," "what's on your menu," "can I see ...," sending tips without prior negotiation.
**Exit signals:** Negotiating a specific purchase → `negotiating`. Paying → `paid`.

### `negotiating`
**Definition:** Active discussion of a specific offer — custom video, PPV bundle, sub upgrade, etc. Price discussed.
**Entry signals:** Specific offer agreed in principle, awaiting payment confirmation.
**Exit signals:** Payment received → `paid`. Customer ghosts mid-negotiation → stays here until 14d dormant, then `cold`.

### `paid`
**Definition:** Purchase confirmed. Bot must not reply. Operator handles delivery and any follow-up.
**Entry signals:** Payment screenshot, "sent," tip received, custom confirmed paid.
**Exit signals:** Operator marks delivery complete (Phase 7 mechanism) → `post_purchase`.
**Bot behavior:** `bot_enabled` is forced off internally regardless of operator setting. Logged but no reply.

### `post_purchase`
**Definition:** Delivery done, upsell window open. Customer has demonstrated they pay; treat as high value.
**Entry signals:** Operator-set after delivery, or LLM-inferred when customer follows up "loved it" etc.
**Exit signals:** New buying inquiry → `hot`. Long dormancy → `cold` after 30d.

## Flags

### `timewaster`
Boolean. True when customer chats endlessly without intent to buy. Multiple long sessions with no buying signals, or explicit "I don't pay for content" type statements. Routing in Phase 7 will deprioritize or ignore these.

### `human_active`
Boolean. True when operator has taken over the chat. Bot does not reply while true. Set automatically when bot detects outbound message it didn't send (Phase 5+ mechanism), or manually via UI/control chat.

## Threat events (not a category or flag)

Detected by signal detector or classifier when serious real-world content appears: doxing references, self-harm mentions, real threats. Fires an operator alert, does not change category or block bot. Operator decides what to do.

## Bootstrap and resurface

- **`bootstrap_completed_at`:** Timestamp set when initial history ingestion finishes. Null = chat was seen live from first message.
- **`bot_enabled`:** Default false for bootstrapped chats (operator must greenlight). Default true for chats seen from first message.
- **`last_resurface_at`:** Set when a previously-classified chat receives a new message after >14 days of silence. Classifier is re-run with explicit "this is a returning customer" context.

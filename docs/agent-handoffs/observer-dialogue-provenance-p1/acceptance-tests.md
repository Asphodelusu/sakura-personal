# Acceptance Tests — Observer Dialogue Provenance P1

## Dialogue authority

- Persisted history IDs strictly order rows that share the same second.
- User, ordinary assistant, proactive assistant, and relationship assistant retain their roles/channels.
- VLM input contains current visual evidence but not prior Observer reasons/comments or claims that the user did not answer.
- Decision input keeps the latest six folded turns and adds bounded short-lived proactive exchange evidence.

## Exchange lifecycle

- Successfully persisted screen/relationship speech creates one anchor after all segment IDs exist.
- Missing, zero, partial, cancelled, or undisplayed speech creates no anchor.
- No later user row means `awaiting_reply`.
- Any later user row means `engaged`, including indirect, uncertain, questioning, or apparently unrelated text.
- `engaged` is never rendered as accepted, rejected, agreed, or settled; actual reply text remains available to the decision model.
- The first ordinary assistant follow-up after the reply is attached; proactive/relationship speech does not masquerade as that follow-up.
- TTL is 1200 seconds, at most five anchors exist, and at most three views are rendered.
- Provider/history failure fails closed for repetition and never recreates an unanswered claim.
- Restart creates an empty exchange ledger.

## Production regression shape

- A proactive question receives an indirect user answer and an ordinary Sakura acknowledgement.
- More than six unrelated turns then occur.
- The normal recent block may omit the old exchange, but the exchange view still says the user responded and includes the actual answer/follow-up.
- A newer ordinary correction or acknowledgement outranks older sensory/Observer hypotheses.
- A genuinely unanswered proactive question remains representable without inferring the user's intent.

## Cost, safety, and compatibility

- No new LLM, embedding, web, or API call is added.
- VLM prompt size should decrease or remain bounded; decision prompt adds at most three clipped views.
- No schema migration, persistent ledger file, production data write, or role/persona change.
- Existing screen/relationship arbitration, idle, cooldown, silence backoff, generation cancellation, and visual dedup tests remain green.
- Diagnostics contain counts/states/IDs/ages/timing only, not dialogue or visual bodies.
- Focused gate and `git diff --check` pass.

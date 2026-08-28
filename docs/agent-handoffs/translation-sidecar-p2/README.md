# Translation sidecar P2

Complete the production path that Phase 1 intentionally left unbound.

The normal chat model still may return `zh`. Those replies remain the zero-extra-call fast
path. Only Chinese-subtitle replies whose text-bearing segments lack `zh` use the configured
`chat_fast` client as a translation sidecar. Japanese TTS must continue immediately, while
the Chinese bubble must not expose untranslated Japanese before translation succeeds or the
bounded fallback gate expires.

This batch is implementation only. It must not change persona prompts, ordinary reply
content, TTS text, Observer/relationship behavior, API keys, character files, or runtime
private data.

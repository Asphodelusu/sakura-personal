# Acceptance tests

- Normal replies with complete `zh` remain byte-for-behavior fast path with no sidecar call.
- Missing Chinese subtitles use the explicitly injected fast client only when enabled and in
  Chinese subtitle mode.
- Japanese TTS is not delayed by translation.
- No Japanese text appears in the Chinese bubble before success, failure, or gate timeout.
- Success updates live segments and persisted history; stale work cannot overwrite new work.
- Invalid/empty model output performs one bounded retry, then degrades safely.
- Failure/timeout cannot leave the reply sequence, busy state, or Observer permanently stuck.
- No credentials or dialogue bodies enter new logs.
- Focused and full project gates pass in `.venv`.

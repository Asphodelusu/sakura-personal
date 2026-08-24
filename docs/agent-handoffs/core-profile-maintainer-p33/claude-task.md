# Claude Code Read-only Task — P3.3 Architecture Check

Read only:

- `docs/superpowers/specs/2026-08-24-core-profile-maintainer-design.md`
- `docs/superpowers/plans/2026-08-24-core-profile-maintainer.md`
- `app/agent/core_profile_candidates.py`
- relevant P3.2 section-patch methods/helpers in `app/agent/memory.py`
- `app/agent/memory_curator.py` only to identify existing API-client/JSON conventions
- `app/config/settings_service.py`
- `data/config/system_config.yaml`

Do not edit files, read runtime/private memory/history/logs, or inspect unrelated modules.

Produce a concise implementation contract for P3.3 covering:

- classes and public methods for scheduler/state/maintainer;
- exact model input boundary and JSON operation parser;
- deterministic validators and which evidence each operation may use;
- cooldown, explicit bypass, stale eligibility, single-flight, per-scope pause;
- queue status transitions and failure atomicity;
- metadata-only metrics and token usage;
- config loading with backend-only defaults;
- test seams and likely integration risks.

Challenge the design where necessary, but preserve zero calls without eligible candidates and zero conversation-path latency.


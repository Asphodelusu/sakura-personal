# Test runtime-log isolation

Pytest currently inherits the user's enabled file-debug setting. Some Observer and
relationship tests call the real `debug_log`, whose module-level `_FILE_LOG_PATH`
points at `data/logs/sakura-runtime.log`. A focused run of relationship/fallback tests
added 17 fake production-log rows, including `ObserverLedger` samples with stub models
and near-zero timings. This corrupts real cost/effect measurement and rotates user logs.

The fix belongs to test infrastructure, not production logging. Redirect test file
logging to pytest-owned temporary storage and close the cached rotating handler at
test boundaries. Tests that explicitly override `_FILE_LOG_PATH` must keep working.


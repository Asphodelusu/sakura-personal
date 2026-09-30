import pytest

from app.core_host.plugin_host_services import HostServiceError, _ContextHostService

HANDLE = "cb_" + "a" * 32


def _service(calls):
    def invoke(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        return []

    return _ContextHostService(invoke, lambda _request: {}, lambda _providers: None)


def test_context_provider_uses_default_deadline_when_undeclared() -> None:
    calls = []
    service = _service(calls)
    service.call("register", [{"providerId": "fixture"}, HANDLE])
    assert service.providers()[0].build_context(object()) == ()
    assert calls == [None]


def test_declared_context_deadline_reaches_worker_callback() -> None:
    calls = []
    service = _service(calls)
    service.call("register", [{"providerId": "fixture", "timeoutSeconds": 6}, HANDLE])
    service.providers()[0].build_context(object())
    assert calls == [6.0]


@pytest.mark.parametrize("timeout", [0, -1, 10.5, True, float("inf"), "6"])
def test_context_deadline_rejects_invalid_limits(timeout) -> None:
    service = _service([])
    with pytest.raises(HostServiceError, match="CONTEXT_DESCRIPTOR_INVALID"):
        service.call("register", [{"providerId": "fixture", "timeoutSeconds": timeout}, HANDLE])

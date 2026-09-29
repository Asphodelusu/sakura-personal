import inspect

import pytest

from plugins.builtin.sakura_mem0.memory import _DisabledGrpcModule


def test_disabled_grpc_does_not_fabricate_module_metadata():
    module = _DisabledGrpcModule("synthetic_grpc")
    assert not hasattr(module, "__file__")
    assert not hasattr(module, "__wrapped__")
    with pytest.raises(TypeError):
        inspect.getfile(module)
    assert module.Channel is module.Channel

import pytest
from steady_helpers import synthetic_context

from petroleum_rto.assistant import native_tools


@pytest.fixture(autouse=True)
def offline_hysys_read(monkeypatch):
    monkeypatch.setattr(native_tools, "read_context", synthetic_context)

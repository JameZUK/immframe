import pytest


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Keep session keys and block lists out of the real ~/.local/state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

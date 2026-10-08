import pytest
from src.env import env_str

class TestEnvStr:
    MADE_UP_DEFAULT = "N/A"
    def test_unset_key_is_default(self, monkeypatch):
        MADE_UP_KEY = "TEST_ENV_STR"
        monkeypatch.delenv(MADE_UP_KEY, raising=False)
        assert env_str(MADE_UP_KEY, self.MADE_UP_DEFAULT) == self.MADE_UP_DEFAULT

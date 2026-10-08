import pytest
from src.env import env_str

class TestEnvStr:
    MADE_UP_KEY = "TEST_ENV_STR"
    MADE_UP_DEFAULT = "N/A"
    def test_unset_key_is_default(self, monkeypatch):
        monkeypatch.delenv(self.MADE_UP_KEY, raising=False)
        assert env_str(self.MADE_UP_KEY, self.MADE_UP_DEFAULT) == self.MADE_UP_DEFAULT

    def test_emptystring_is_default(self, monkeypatch):
        monkeypatch.setenv(self.MADE_UP_KEY, "")
        assert env_str(self.MADE_UP_KEY, self.MADE_UP_DEFAULT) == self.MADE_UP_DEFAULT

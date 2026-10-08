import pytest
from src.env import env_str, env_int, env_float, env_flag

MADE_UP_KEY = "TEST_ENV_STR"

class TestEnvStr:
    DEFAULT_STR = "N/A"
    def test_unset_key_is_default(self, monkeypatch):
        monkeypatch.delenv(MADE_UP_KEY, raising=False)
        assert env_str(MADE_UP_KEY, self.DEFAULT_STR) == self.DEFAULT_STR

    def test_empty_value_is_default(self, monkeypatch):
        monkeypatch.setenv(MADE_UP_KEY, "")
        assert env_str(MADE_UP_KEY, self.DEFAULT_STR) == self.DEFAULT_STR

    def test_whitespace_is_default(self, monkeypatch):
        monkeypatch.setenv(MADE_UP_KEY, "   ")
        assert env_str(MADE_UP_KEY, self.DEFAULT_STR) == self.DEFAULT_STR

    def test_striped_value(self, monkeypatch):
        TEST_VAL_STR = "abc"
        monkeypatch.setenv(MADE_UP_KEY, f" {TEST_VAL_STR} ")
        assert env_str(MADE_UP_KEY, self.DEFAULT_STR) == TEST_VAL_STR

class TestEnvInt:
    DEFAULT_INT = 0
    def test_is_number(self, monkeypatch):
        TEST_VAL_INT = 15
        monkeypatch.setenv(MADE_UP_KEY, str(TEST_VAL_INT))
        assert env_int(MADE_UP_KEY, self.DEFAULT_INT) == TEST_VAL_INT

    def test_blank_is_default(self, monkeypatch):
        monkeypatch.setenv(MADE_UP_KEY, "")
        assert env_int(MADE_UP_KEY, self.DEFAULT_INT) == self.DEFAULT_INT

    def test_str_is_default_and_warning(self, monkeypatch, caplog):
        TEST_VAL_STR = "abc"
        monkeypatch.setenv(MADE_UP_KEY, TEST_VAL_STR)
        assert env_int(MADE_UP_KEY, self.DEFAULT_INT) == self.DEFAULT_INT
        assert "not an integer" in caplog.text

class TestEnvFloat:
    DEFAULT_FLOAT = 0.00
    def test_is_number(self, monkeypatch):
        TEST_VAL_FLOAT = 15.5
        monkeypatch.setenv(MADE_UP_KEY, str(TEST_VAL_FLOAT))
        assert env_float(MADE_UP_KEY, self.DEFAULT_FLOAT) == TEST_VAL_FLOAT

    def test_blank_is_default(self, monkeypatch):
        monkeypatch.setenv(MADE_UP_KEY, "")
        assert env_float(MADE_UP_KEY, self.DEFAULT_FLOAT) == self.DEFAULT_FLOAT

    def test_str_is_default_and_warning(self, monkeypatch, caplog):
        TEST_VAL_STR = "abc"
        monkeypatch.setenv(MADE_UP_KEY, TEST_VAL_STR)
        assert env_float(MADE_UP_KEY, self.DEFAULT_FLOAT) == self.DEFAULT_FLOAT
        assert "not a number" in caplog.text

class TestEnvFlag:
    @pytest.mark.parametrize("val,exp", [
        ("1", True), ("true", True), ("yes", True), ("y", True), ("on", True), ("TRUE", True),
        ("0", False), ("no", False), ("abc", False), ("NO", False)
        ])
    def test_confirm_bool(self, monkeypatch, val, exp):
        monkeypatch.setenv(MADE_UP_KEY, val)
        assert env_flag(MADE_UP_KEY) is exp

    def test_blank_is_default(self, monkeypatch):
        monkeypatch.setenv(MADE_UP_KEY, "")
        assert env_flag(MADE_UP_KEY) is False
        assert env_flag(MADE_UP_KEY, True) is True

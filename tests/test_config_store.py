"""Typed config coercion — these values gate logins and maintenance mode."""

import pandas as pd
import pytest

from core.config_store import ConfigSpec, _coerce, config_spec, get_config


class _FailingDb:
    def query(self, *args, **kwargs):
        raise RuntimeError("no database in the regression suite")


class _FrameDb:
    def __init__(self, frame):
        self._frame = frame

    def query(self, sql, params=None):
        return self._frame


def test_boolean_coercion_accepts_common_forms_and_rejects_junk():
    spec = ConfigSpec("runtime", "boolean")
    assert _coerce("true", spec) is True
    assert _coerce("0", spec) is False
    assert _coerce("", spec) is False
    with pytest.raises(ValueError):
        _coerce("maybe", spec)


def test_number_coercion_keeps_integers_and_rejects_booleans():
    spec = ConfigSpec("finance", "number")
    assert _coerce("4", spec) == 4
    assert isinstance(_coerce("4", spec), int)
    assert _coerce("3.5", spec) == 3.5
    with pytest.raises(ValueError):
        _coerce(True, spec)


def test_collection_coercion_enforces_shape():
    assert _coerce('["a"]', ConfigSpec("access", "array")) == ["a"]
    assert _coerce("", ConfigSpec("access", "array")) == []
    with pytest.raises(ValueError):
        _coerce('{"a":1}', ConfigSpec("access", "array"))


def test_unknown_keys_are_rejected_unless_explicitly_legacy():
    with pytest.raises(KeyError):
        config_spec("made_up_key")
    assert config_spec("made_up_key", allow_legacy=True).namespace == "legacy"
    with pytest.raises(KeyError):
        config_spec("bandwidth_3gb_push_x")


def test_get_config_returns_default_when_store_is_unreachable():
    assert get_config(_FailingDb(), "maintenance_mode", default=False) is False
    assert get_config(_FailingDb(), "ai_fund_low_balance_hkd", default=100) == 100


def test_get_config_reads_typed_value():
    db = _FrameDb(pd.DataFrame([{"value": True}]))
    assert get_config(db, "maintenance_mode", default=False) is True

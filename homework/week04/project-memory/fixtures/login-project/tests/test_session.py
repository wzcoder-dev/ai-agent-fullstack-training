import pytest

from session import create_session, is_session_valid


def test_session_contains_user_and_expiry():
    assert create_session("alice", 100) == {"user_id": "alice", "expires_at": 1900}


def test_session_valid_before_expiry():
    session = create_session("alice", 100)
    assert is_session_valid(session, 1899)


def test_session_invalid_at_expiry():
    assert not is_session_valid(create_session("alice", 100), 1900)


def test_session_invalid_after_expiry():
    assert not is_session_valid(create_session("alice", 100), 1901)


def test_empty_user_rejected():
    with pytest.raises(ValueError):
        create_session("", 100)

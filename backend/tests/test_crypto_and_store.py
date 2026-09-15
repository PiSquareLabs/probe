"""Credentials must survive a round trip and never leak in the masked view."""

import json

import pytest

from app.crypto import SecretBox, mask
from app.errors import ConfigurationError
from app.store import ConnectionStore


def test_round_trip_with_passphrase():
    box = SecretBox("a-human-typed-passphrase")
    token = "ATATT3xFfGF0T_secret_value"
    assert box.decrypt(box.encrypt(token)) == token


def test_round_trip_with_real_fernet_key():
    from cryptography.fernet import Fernet

    box = SecretBox(Fernet.generate_key().decode())
    assert box.decrypt(box.encrypt("hunter2")) == "hunter2"


def test_ciphertext_does_not_contain_plaintext():
    box = SecretBox("k")
    assert "secret_value" not in box.encrypt("secret_value")


def test_empty_secret_key_is_rejected():
    with pytest.raises(ConfigurationError):
        SecretBox("")


def test_wrong_key_reports_a_useful_error():
    ciphertext = SecretBox("key-one").encrypt("value")
    with pytest.raises(ConfigurationError, match="cannot be decrypted"):
        SecretBox("key-two").decrypt(ciphertext)


def test_mask_keeps_the_ends_only():
    assert mask("ATATT3xFfGF0Tabcdef9c1f4e2a") == "ATATT3…4e2a"
    assert mask("") == ""


def test_short_values_are_fully_masked():
    assert "abc" not in mask("abc")


def test_store_persists_and_reloads(setup_request, state_path):
    store = ConnectionStore(state_path, SecretBox("k"))
    store.save(setup_request)

    assert store.is_configured()
    assert store.jira().api_token == setup_request.jira.api_token
    assert store.elastic().api_key == setup_request.elastic.api_key
    assert store.connector().index_name == "search-jira-probe"


def test_token_is_encrypted_on_disk(setup_request, state_path):
    store = ConnectionStore(state_path, SecretBox("k"))
    store.save(setup_request)

    raw = json.loads(open(state_path).read())
    assert raw["jira"]["api_token"].startswith("enc::")
    assert setup_request.jira.api_token not in open(state_path).read()
    # Non-secret fields stay readable so the file can be inspected.
    assert raw["jira"]["project_key"] == "PROBE"


def test_state_view_masks_every_secret(setup_request, state_path):
    store = ConnectionStore(state_path, SecretBox("k"))
    store.save(setup_request)

    state = store.state()
    dumped = state.model_dump_json()
    assert setup_request.jira.api_token not in dumped
    assert setup_request.elastic.api_key not in dumped
    assert "…" in state.jira.api_token
    assert state.jira.project_key == "PROBE"


def test_clear_removes_state(setup_request, state_path):
    store = ConnectionStore(state_path, SecretBox("k"))
    store.save(setup_request)
    store.clear()
    assert not store.is_configured()
    assert store.state().configured is False

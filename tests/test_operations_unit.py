"""No-infrastructure security contracts for operator-facing telemetry."""

from database.operations import _safe_json, git_metadata, redact_message


def test_redact_message_removes_common_secret_shapes():
    message = (
        "failed postgresql://brain:supersecret@127.0.0.1/db "
        "password=hunter2 Authorization:Bearer-token Bearer abc.def.ghi?apiKey=lastsecret"
    )
    redacted = redact_message(message)
    assert "supersecret" not in redacted
    assert "hunter2" not in redacted
    assert "Bearer-token" not in redacted
    assert "abc.def.ghi" not in redacted
    assert "lastsecret" not in redacted
    assert redacted.count("<redacted>") >= 4


def test_nested_telemetry_details_are_sanitized_and_json_safe():
    value = {"items": ["token=abc", {"dsn": "postgresql://u:p@db/x"}], "ok": True}
    safe = _safe_json(value)
    assert safe == {
        "items": ["token=<redacted>", {"dsn": "postgresql://u:<redacted>@db/x"}],
        "ok": True,
    }


def test_git_metadata_never_claims_unknown_worktree_is_clean():
    metadata = git_metadata()
    assert metadata["git_commit"]
    assert metadata["git_dirty"] in (True, False, None)
    if metadata["git_dirty"]:
        assert metadata["git_changed_files"] > 0
        assert len(metadata["git_worktree_fingerprint"]) == 64
    elif metadata["git_dirty"] is False:
        assert metadata["git_changed_files"] == 0
        assert metadata["git_worktree_fingerprint"] is None

"""Archive versions are fixed at save time; SMTP semantics remain independent."""

import hashlib
import smtplib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from src.notification import email_notifier
from src.notification.email_notifier import EmailNotifier


@pytest.fixture
def mail(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIP_EMAIL", "true")
    monkeypatch.setenv("SKIP_NOTIFICATIONS", "true")
    # Enable only the isolated adapter, with SMTP constructors replaced below.
    monkeypatch.setattr(email_notifier, "channel_enabled", lambda *_: True)
    blocked = Mock(side_effect=AssertionError("real network forbidden"))
    monkeypatch.setattr("requests.sessions.Session.request", blocked)
    notifier = EmailNotifier(
        {
            "email": {
                "smtp_server": "smtp.invalid",
                "sender_email": "from@example.invalid",
                "receiver_email": "to@example.invalid",
                "sender_password": "synthetic",
                "archive_dir": str(tmp_path),
            }
        }
    )
    connection = MagicMock()
    server = connection.__enter__.return_value
    server.send_message.return_value = {}
    monkeypatch.setattr(smtplib, "SMTP", Mock(return_value=connection))
    monkeypatch.setattr(smtplib, "SMTP_SSL", Mock(return_value=connection))
    yield notifier, server
    blocked.assert_not_called()


def test_same_second_same_subject_preserves_distinct_exact_archive_bytes(
    mail, monkeypatch
):
    notifier, server = mail
    fixed = datetime(2026, 11, 9, 9, 50, 0).astimezone()
    monkeypatch.setattr(email_notifier, "datetime", SimpleNamespace(now=lambda: fixed))
    first = notifier._send_email("same subject", "<body>first</body>")
    second = notifier._send_email("same subject", "<body>second</body>")
    assert first["archive"] != second["archive"]
    assert "first" in Path(first["archive"]).read_text(encoding="utf-8")
    assert "second" in Path(second["archive"]).read_text(encoding="utf-8")
    for receipt in (first, second):
        assert receipt["status"] == "sent"
        assert (
            receipt["archive_sha256"]
            == hashlib.sha256(Path(receipt["archive"]).read_bytes()).hexdigest()
        )
    assert server.send_message.call_count == 2


def test_even_a_forced_name_collision_cannot_overwrite_saved_copy(mail, monkeypatch):
    notifier, _ = mail
    fixed = datetime(2026, 11, 9, 9, 50, 0).astimezone()
    monkeypatch.setattr(email_notifier, "datetime", SimpleNamespace(now=lambda: fixed))
    monkeypatch.setattr(email_notifier, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    path, digest = notifier._save_email_copy("subject", "original")
    assert notifier._save_email_copy("subject", "replacement") is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert "replacement" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "error,status",
    [
        (None, "sent"),
        (TimeoutError("uncertain"), "unknown"),
        (smtplib.SMTPDataError(550, b"rejected"), "failed"),
    ],
)
def test_archive_hash_failure_never_changes_smtp_outcome(
    mail, monkeypatch, error, status
):
    notifier, server = mail
    server.send_message.side_effect = error
    monkeypatch.setattr(
        email_notifier.hashlib, "sha256", Mock(side_effect=OSError("hash unavailable"))
    )
    result = notifier._send_email("subject", "<body>saved without digest</body>")
    assert result["status"] == status
    assert result["archive_sha256"] is None
    assert Path(result["archive"]).exists()
    server.send_message.assert_called_once()


@pytest.mark.parametrize(
    "error,status",
    [
        (TimeoutError("uncertain"), "unknown"),
        (smtplib.SMTPDataError(550, b"rejected"), "failed"),
    ],
)
def test_failure_and_unknown_receipts_retain_original_archive_digest(
    mail, error, status
):
    notifier, server = mail
    server.send_message.side_effect = error
    result = notifier._send_email("subject", "<body>saved evidence</body>")
    assert result["status"] == status
    assert (
        result["archive_sha256"]
        == hashlib.sha256(Path(result["archive"]).read_bytes()).hexdigest()
    )

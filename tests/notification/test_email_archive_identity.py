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


def test_sent_pdf_and_attachment_are_archived_as_exact_mime_bytes(mail):
    notifier, server = mail
    pdf = b"%PDF-1.7\ncontrolled report bytes\n%%EOF"
    receipt = notifier._send_email(
        "report",
        "<body>body</body>",
        pdf_bytes=pdf,
        attachments=[
            {
                "filename": "reader.html",
                "content": "<p>原文</p>".encode(),
                "content_type": "text/html",
            }
        ],
    )
    sent = server.send_message.call_args.args[0]
    parts = [
        part for part in sent.walk() if part.get_content_disposition() == "attachment"
    ]
    archived = receipt.get("attachments", [])
    assert len(archived) == len(parts) == 2
    for saved, part in zip(archived, parts):
        raw = part.get_payload(decode=True)
        assert saved["archive_status"] == "saved"
        assert saved["filename"] == part.get_filename()
        assert saved["content_type"] == part.get_content_type()
        assert saved["bytes"] == len(raw)
        assert Path(saved["archive"]).read_bytes() == raw
        assert saved["archive_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["status"] == "sent"


@pytest.mark.parametrize(
    "phase,status",
    [
        ("connect", "failed"),
        ("login", "failed"),
        ("reject", "failed"),
        ("submit", "unknown"),
        ("quit", "sent"),
    ],
)
def test_attachment_evidence_preserves_smtp_stage_outcome(mail, phase, status):
    notifier, server = mail
    connection = smtplib.SMTP_SSL.return_value
    if phase == "connect":
        smtplib.SMTP_SSL.side_effect = TimeoutError("connection failed")
    elif phase == "login":
        server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"denied")
    elif phase == "reject":
        server.send_message.side_effect = smtplib.SMTPDataError(550, b"denied")
    elif phase == "submit":
        server.send_message.side_effect = TimeoutError("ack lost")
    else:
        connection.__exit__.side_effect = TimeoutError("quit lost")
    receipt = notifier._send_email("report", "body", pdf_bytes=b"%PDF-1.7\n%%EOF")
    assert receipt["status"] == status
    assert receipt["attachments"][0]["archive_status"] == "saved"
    assert Path(receipt["attachments"][0]["archive"]).read_bytes() == b"%PDF-1.7\n%%EOF"
    assert server.send_message.call_count == (0 if phase in {"connect", "login"} else 1)


@pytest.mark.parametrize(
    "error,status",
    [
        (None, "sent"),
        (TimeoutError("ack lost"), "unknown"),
        (smtplib.SMTPDataError(550, b"denied"), "failed"),
    ],
)
def test_attachment_archive_failure_does_not_reclassify_smtp_result(
    mail, monkeypatch, error, status
):
    notifier, server = mail
    server.send_message.side_effect = error
    original = Path.open

    def fail_attachment(path, *args, **kwargs):
        if path.parent.name == ".attachments":
            raise OSError("archive unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_attachment)
    receipt = notifier._send_email("report", "body", pdf_bytes=b"%PDF-1.7\n%%EOF")
    assert receipt["status"] == status
    if status == "sent":
        assert receipt["smtp_accepted"] is True
    assert receipt["attachments"][0]["archive_status"] == "failed"
    assert "archive" not in receipt["attachments"][0]
    server.send_message.assert_called_once()


def test_attachment_name_collision_never_overwrites_history(mail, monkeypatch):
    notifier, _ = mail
    monkeypatch.setattr(email_notifier, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    first = notifier._send_email("report", "body", pdf_bytes=b"%PDF-first")
    second = notifier._send_email("report", "body", pdf_bytes=b"%PDF-second")
    assert first["attachments"][0]["archive_status"] == "saved"
    assert second["attachments"][0]["archive_status"] == "failed"
    assert second["status"] == "sent"
    saved = first["attachments"][0]
    assert Path(saved["archive"]).read_bytes() == b"%PDF-first"
    assert (
        hashlib.sha256(Path(saved["archive"]).read_bytes()).hexdigest()
        == saved["archive_sha256"]
    )


@pytest.mark.parametrize("failed", [False, True])
def test_daily_pdf_generation_gap_is_partial_with_html_smtp_sent(
    mail, monkeypatch, failed
):
    import pandas as pd

    notifier, server = mail
    generator = (
        Mock(side_effect=RuntimeError("render failed"))
        if failed
        else Mock(return_value=None)
    )
    monkeypatch.setattr(notifier, "_generate_daily_pdf", generator)
    monkeypatch.setattr(notifier, "_build_email_body", lambda *_a, **_k: "<p>daily</p>")
    monkeypatch.setattr(email_notifier, "generate_return_curve_png", lambda *_: None)
    session = SimpleNamespace(
        get_all_dataframe=lambda: pd.DataFrame(), announcements={}
    )
    receipt = notifier.send_daily_report_from_session(session)
    assert receipt["status"] == "partial"
    assert receipt["smtp_status"] == "sent"
    assert receipt["pieces"][0]["status"] == "sent"
    assert receipt["pdf"]["status"] == "failed"
    server.send_message.assert_called_once()

"""Regression coverage for delivered daily and brief email content."""

import base64
from email import policy
from email.parser import BytesParser
from unittest.mock import MagicMock

import pytest

from src.notification.email_notifier import EmailNotifier


@pytest.fixture
def notifier(monkeypatch, tmp_path):
    instance = EmailNotifier(
        {
            "email": {
                "smtp_server": "smtp.invalid",
                "sender_email": "test@example.invalid",
                "sender_password": "test-password",
                "receiver_email": "test@example.invalid",
                "archive_dir": str(tmp_path),
            }
        }
    )
    monkeypatch.setattr(instance, "_get_server_info", dict)
    return instance


@pytest.mark.parametrize("with_charts", [False, True])
@pytest.mark.parametrize("with_pdf", [False, True])
def test_sent_mime_has_one_encoding_and_decodable_inline_images(
    notifier, monkeypatch, with_charts, with_pdf
):
    transport = MagicMock()
    monkeypatch.setattr("smtplib.SMTP_SSL", transport)
    monkeypatch.setattr(
        "src.notification.email_notifier.channel_enabled", lambda *args: True
    )
    monkeypatch.setattr(notifier, "_save_email_copy", lambda *args: "mock.html")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZk"
        "AAAAASUVORK5CYII="
    )
    body = "<html><body>买入信号 &amp; 图表</body></html>"
    if with_charts:
        body = body.replace(
            "</body>", '<img src="cid:chart001"><img src="cid:chart002"></body>'
        )
    notifier._send_email(
        "中文日报",
        body,
        chart_png_bytes=png if with_charts else None,
        portfolio_chart_dict={"overview": png} if with_charts else None,
        pdf_bytes=b"%PDF-test" if with_pdf else None,
    )

    sent = transport.return_value.__enter__.return_value.send_message.call_args.args[0]
    parsed = BytesParser(policy=policy.default).parsebytes(sent.as_bytes())
    html_parts = [
        part for part in parsed.walk() if part.get_content_type() == "text/html"
    ]
    assert len(html_parts) == 1
    html_part = html_parts[0]
    assert len(html_part.get_all("Content-Transfer-Encoding")) == 1
    assert html_part.get_content() == body
    image_parts = [
        part for part in parsed.walk() if part.get_content_type() == "image/png"
    ]
    assert [part["Content-ID"] for part in image_parts] == (
        ["<chart001>", "<chart002>"] if with_charts else []
    )
    assert all(part.get_payload(decode=True) == png for part in image_parts)
    pdf_parts = [
        part for part in parsed.walk() if part.get_content_type() == "application/pdf"
    ]
    assert len(pdf_parts) == int(with_pdf)
    assert not any(part.defects for part in parsed.walk())

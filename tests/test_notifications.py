import pytest

from app.main import build_notification_config, build_notification_config_from_form, notification_form_from_item
from app.models import NotificationChannel


def test_build_email_notification_config():
    config = build_notification_config(
        channel_type="email",
        email_host=" smtp.example.com ",
        email_port=465,
        email_use_tls=False,
        email_username="user@example.com",
        email_password="secret",
        email_sender="",
        email_recipients="admin@example.com",
        email_subject="告警 $message",
        email_body="$channel_name $message",
    )

    assert config["host"] == "smtp.example.com"
    assert config["port"] == 465
    assert config["use_tls"] is False
    assert config["sender"] == "user@example.com"
    assert config["recipients"] == "admin@example.com"


def test_build_http_notification_config():
    config = build_notification_config(
        channel_type="http",
        http_method="post",
        http_url=" https://example.test/webhook ",
        http_headers_json='{"Content-Type": "application/json"}',
        http_body='{"text":"$message"}',
    )

    assert config == {
        "method": "POST",
        "url": "https://example.test/webhook",
        "headers": {"Content-Type": "application/json"},
        "body": '{"text":"$message"}',
    }


def test_http_notification_rejects_invalid_headers():
    with pytest.raises(ValueError, match="Headers JSON"):
        build_notification_config(channel_type="http", http_url="https://example.test", http_headers_json="{bad")


def test_edit_email_notification_preserves_password_when_blank():
    config = build_notification_config_from_form(
        {
            "channel_type": "email",
            "email_host": "smtp.example.com",
            "email_port": 587,
            "email_use_tls": True,
            "email_username": "user@example.com",
            "email_password": "",
            "email_sender": "user@example.com",
            "email_recipients": "admin@example.com",
            "email_subject": "subject",
            "email_body": "body",
        },
        existing={"password": "old-secret"},
    )

    assert config["password"] == "old-secret"


def test_notification_form_from_http_item_pretty_prints_json():
    item = NotificationChannel(
        name="webhook",
        channel_type="http",
        enabled=True,
        config_json='{"method":"POST","url":"https://example.test","headers":{"X-Test":"1"},"body":"{\\"text\\":\\"$message\\"}"}',
    )

    form = notification_form_from_item(item)

    assert form["name"] == "webhook"
    assert form["channel_type"] == "http"
    assert '"X-Test": "1"' in form["http_headers_json"]
    assert '"text": "$message"' in form["http_body"]

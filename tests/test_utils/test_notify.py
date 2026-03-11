import json
import os
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

_NOTIFY_FILE = Path(__file__).resolve().parents[2] / "specforge" / "notify.py"
_NOTIFY_SPEC = spec_from_file_location("specforge_notify_module", _NOTIFY_FILE)
_NOTIFY_MODULE = module_from_spec(_NOTIFY_SPEC)
assert _NOTIFY_SPEC.loader is not None
_NOTIFY_SPEC.loader.exec_module(_NOTIFY_MODULE)

GmailHook = _NOTIFY_MODULE.GmailHook
NotificationHook = _NOTIFY_MODULE.NotificationHook
NotificationManager = _NOTIFY_MODULE.NotificationManager
create_hook = _NOTIFY_MODULE.create_hook


class _DummyHook(NotificationHook):
    def __init__(self, should_fail=False, **kwargs):
        super().__init__(**kwargs)
        self.should_fail = should_fail
        self.sent_payloads = []

    def _send(self, payload):
        if self.should_fail:
            raise RuntimeError("dummy failure")
        self.sent_payloads.append(payload)


class _FakeSMTP:
    last_instance = None

    def __init__(self, host, port, timeout=0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.messages = []
        self.quit_called = False
        _FakeSMTP.last_instance = self

    def ehlo(self):
        return None

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.messages.append(message)

    def quit(self):
        self.quit_called = True


class _FakeWebhookResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class TestNotify(unittest.TestCase):

    def test_gmail_hook_defaults(self):
        hook = create_hook(
            {
                "type": "gmail",
                "username": "bot@gmail.com",
                "to": ["ops@example.com"],
                "password_env": "SMTP_PASSWORD",
            }
        )

        self.assertIsInstance(hook, GmailHook)
        self.assertEqual(hook.smtp_host, "smtp.gmail.com")
        self.assertEqual(hook.smtp_port, 587)
        self.assertTrue(hook.use_starttls)
        self.assertFalse(hook.use_ssl)

    @patch.dict(os.environ, {"SMTP_PASSWORD": "app-password-123"}, clear=False)
    @patch.object(_NOTIFY_MODULE.smtplib, "SMTP", new=_FakeSMTP)
    def test_smtp_hook_send_email(self):
        hook = create_hook(
            {
                "type": "smtp",
                "name": "smtp_main",
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "username": "bot@gmail.com",
                "password_env": "SMTP_PASSWORD",
                "to": ["user@example.com"],
                "from": "bot@gmail.com",
                "use_starttls": True,
                "events": ["RUN_COMPLETED"],
            }
        )

        sent = hook.send(
            event="RUN_COMPLETED",
            title="Training done",
            message="run finished",
            context={"run_id": "run-1"},
        )

        smtp = _FakeSMTP.last_instance
        self.assertTrue(sent)
        self.assertIsNotNone(smtp)
        self.assertEqual(smtp.host, "smtp.gmail.com")
        self.assertEqual(smtp.port, 587)
        self.assertTrue(smtp.started_tls)
        self.assertEqual(smtp.login_args, ("bot@gmail.com", "app-password-123"))
        self.assertEqual(len(smtp.messages), 1)
        self.assertTrue(smtp.quit_called)

    @patch.object(_NOTIFY_MODULE, "urlopen")
    def test_webhook_hook_send_json_payload(self, mock_urlopen):
        captured = {}

        def _fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode("utf-8")
            captured["timeout"] = timeout
            return _FakeWebhookResponse(status=200)

        mock_urlopen.side_effect = _fake_urlopen

        hook = create_hook(
            {
                "type": "webhook",
                "url": "https://example.com/hook",
                "timeout": 5,
            }
        )
        sent = hook.send(
            event="RUN_FAILED",
            title="Training failed",
            message="oom",
            context={"step": "TRAIN_ONLINE"},
        )

        self.assertTrue(sent)
        self.assertEqual(captured["url"], "https://example.com/hook")
        self.assertEqual(captured["timeout"], 5)
        payload = json.loads(captured["body"])
        self.assertEqual(payload["event"], "RUN_FAILED")
        self.assertEqual(payload["message"], "oom")
        self.assertEqual(payload["context"]["step"], "TRAIN_ONLINE")

    def test_notification_manager_event_filter_and_error_tolerance(self):
        hook_a = _DummyHook(name="a", events=["RUN_COMPLETED"])
        hook_b = _DummyHook(name="b", should_fail=True)

        manager = NotificationManager([hook_a, hook_b])
        result = manager.notify(
            event="RUN_STARTED",
            title="started",
            message="begin",
            context={"run_id": "x"},
            fail_fast=False,
        )

        self.assertEqual(result["total_hooks"], 2)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(result["errors"]), 1)


if __name__ == "__main__":
    unittest.main()

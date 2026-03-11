import json
import os
import smtplib
import ssl
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.request import Request, urlopen


class NotificationError(RuntimeError):
    """Raised when one or more notification hooks fail."""


@dataclass
class NotificationPayload:
    event: str
    title: str
    message: str
    context: Optional[Dict[str, Any]] = None
    timestamp: float = 0.0


class NotificationHook(ABC):
    """Base class for all notification hooks."""

    def __init__(
        self,
        name: Optional[str] = None,
        enabled: bool = True,
        events: Optional[Sequence[str]] = None,
    ) -> None:
        self.name = name or self.__class__.__name__.lower()
        self.enabled = enabled
        self.events = set(events or [])

    def should_send(self, event: str) -> bool:
        return self.enabled and (not self.events or event in self.events)

    def send(
        self,
        event: str,
        title: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self.should_send(event):
            return False

        payload = NotificationPayload(
            event=event,
            title=title,
            message=message,
            context=context,
            timestamp=time.time(),
        )
        self._send(payload)
        return True

    @abstractmethod
    def _send(self, payload: NotificationPayload) -> None:
        """Send the notification payload through this hook."""


class WebhookHook(NotificationHook):
    """Send notifications to generic JSON webhooks."""

    def __init__(
        self,
        url: str,
        timeout: int = 10,
        headers: Optional[Mapping[str, str]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if not url:
            raise ValueError("WebhookHook requires non-empty `url`.")
        self.url = url
        self.timeout = timeout
        self.headers = dict(headers or {})

    def _send(self, payload: NotificationPayload) -> None:
        body = {
            "event": payload.event,
            "title": payload.title,
            "message": payload.message,
            "context": payload.context or {},
            "timestamp": payload.timestamp,
        }
        data = json.dumps(body).encode("utf-8")
        request = Request(self.url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        for k, v in self.headers.items():
            request.add_header(k, v)

        with urlopen(request, timeout=self.timeout) as resp:  # nosec B310
            status_code = getattr(resp, "status", None)
            if status_code is not None and status_code >= 400:
                raise NotificationError(
                    f"Webhook '{self.name}' returned HTTP status {status_code}."
                )


class SMTPHook(NotificationHook):
    """Send notifications through an SMTP server."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        to: Sequence[str],
        username: Optional[str] = None,
        password: Optional[str] = None,
        password_env: Optional[str] = None,
        from_email: Optional[str] = None,
        use_starttls: bool = True,
        use_ssl: bool = False,
        timeout: int = 20,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if not smtp_host:
            raise ValueError("SMTPHook requires `smtp_host`.")
        if not to:
            raise ValueError("SMTPHook requires at least one recipient in `to`.")
        if use_ssl and use_starttls:
            raise ValueError("`use_ssl` and `use_starttls` cannot both be true.")

        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.password_env = password_env
        self.from_email = from_email or username
        self.to = list(to)
        self.use_starttls = use_starttls
        self.use_ssl = use_ssl
        self.timeout = timeout

    def _resolve_password(self) -> Optional[str]:
        if self.password:
            return self.password
        if self.password_env:
            env_value = os.environ.get(self.password_env)
            if env_value:
                return env_value
        return None

    def _build_email(self, payload: NotificationPayload) -> EmailMessage:
        msg = EmailMessage()
        subject = (
            f"[{payload.event}] {payload.title}" if payload.title else payload.event
        )
        msg["Subject"] = subject
        msg["From"] = self.from_email or "noreply@localhost"
        msg["To"] = ", ".join(self.to)

        lines: List[str] = [payload.message, "", f"event: {payload.event}"]
        if payload.context:
            lines.append("context:")
            lines.append(json.dumps(payload.context, ensure_ascii=False, indent=2))
        msg.set_content("\n".join(lines))
        return msg

    def _send(self, payload: NotificationPayload) -> None:
        msg = self._build_email(payload)
        password = self._resolve_password()

        smtp_client = None
        try:
            if self.use_ssl:
                smtp_client = smtplib.SMTP_SSL(
                    self.smtp_host, self.smtp_port, timeout=self.timeout
                )
            else:
                smtp_client = smtplib.SMTP(
                    self.smtp_host, self.smtp_port, timeout=self.timeout
                )
                smtp_client.ehlo()
                if self.use_starttls:
                    smtp_client.starttls(context=ssl.create_default_context())
                    smtp_client.ehlo()

            if self.username:
                if not password:
                    raise ValueError(
                        f"SMTP hook '{self.name}' requires password or password_env."
                    )
                smtp_client.login(self.username, password)

            smtp_client.send_message(msg)
        finally:
            if smtp_client is not None:
                smtp_client.quit()


class GmailHook(SMTPHook):
    """SMTP notification hook preconfigured for Gmail."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("smtp_host", "smtp.gmail.com")
        kwargs.setdefault("smtp_port", 587)
        kwargs.setdefault("use_starttls", True)
        kwargs.setdefault("use_ssl", False)
        super().__init__(**kwargs)


HookFactory = Callable[[Mapping[str, Any]], NotificationHook]


def _create_webhook_hook(config: Mapping[str, Any]) -> NotificationHook:
    return WebhookHook(
        url=config.get("url", ""),
        timeout=int(config.get("timeout", 10)),
        headers=config.get("headers"),
        name=config.get("name"),
        enabled=bool(config.get("enabled", True)),
        events=config.get("events"),
    )


def _create_smtp_hook(config: Mapping[str, Any]) -> NotificationHook:
    return SMTPHook(
        smtp_host=config.get("smtp_host", ""),
        smtp_port=int(config.get("smtp_port", 587)),
        username=config.get("username"),
        password=config.get("password"),
        password_env=config.get("password_env"),
        from_email=config.get("from"),
        to=config.get("to", []),
        use_starttls=bool(config.get("use_starttls", True)),
        use_ssl=bool(config.get("use_ssl", False)),
        timeout=int(config.get("timeout", 20)),
        name=config.get("name"),
        enabled=bool(config.get("enabled", True)),
        events=config.get("events"),
    )


def _create_gmail_hook(config: Mapping[str, Any]) -> NotificationHook:
    return GmailHook(
        username=config.get("username"),
        password=config.get("password"),
        password_env=config.get("password_env"),
        from_email=config.get("from"),
        to=config.get("to", []),
        timeout=int(config.get("timeout", 20)),
        name=config.get("name"),
        enabled=bool(config.get("enabled", True)),
        events=config.get("events"),
    )


_HOOK_REGISTRY: Dict[str, HookFactory] = {
    "webhook": _create_webhook_hook,
    "smtp": _create_smtp_hook,
    "gmail": _create_gmail_hook,
}


def register_hook_type(hook_type: str, factory: HookFactory) -> None:
    """Register a custom hook factory by type name."""
    if not hook_type:
        raise ValueError("hook_type cannot be empty")
    _HOOK_REGISTRY[hook_type] = factory


def create_hook(config: Mapping[str, Any]) -> NotificationHook:
    """Create one notification hook from a config dictionary."""
    hook_type = str(config.get("type", "")).strip().lower()
    if not hook_type:
        raise ValueError("Notification hook config requires `type`.")
    if hook_type not in _HOOK_REGISTRY:
        supported = ", ".join(sorted(_HOOK_REGISTRY.keys()))
        raise ValueError(f"Unsupported hook type '{hook_type}'. Supported: {supported}")
    return _HOOK_REGISTRY[hook_type](config)


def create_hooks(configs: Sequence[Mapping[str, Any]]) -> List[NotificationHook]:
    """Create multiple hooks from a list of config dictionaries."""
    return [create_hook(cfg) for cfg in configs]


class NotificationManager:
    """Fan-out notification sender for multiple hooks."""

    def __init__(self, hooks: Optional[Sequence[NotificationHook]] = None) -> None:
        self.hooks = list(hooks or [])

    def add_hook(self, hook: NotificationHook) -> None:
        self.hooks.append(hook)

    def notify(
        self,
        event: str,
        title: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        fail_fast: bool = False,
    ) -> Dict[str, Any]:
        sent = 0
        skipped = 0
        errors: List[Dict[str, str]] = []

        for hook in self.hooks:
            try:
                delivered = hook.send(event, title, message, context=context)
                if delivered:
                    sent += 1
                else:
                    skipped += 1
            except Exception as exc:
                error = {
                    "hook": hook.name,
                    "event": event,
                    "error": str(exc),
                }
                errors.append(error)
                if fail_fast:
                    raise NotificationError(str(error)) from exc

        return {
            "event": event,
            "total_hooks": len(self.hooks),
            "sent": sent,
            "skipped": skipped,
            "errors": errors,
        }

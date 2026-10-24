"""Delivering one-time codes: SMS, voice calls and email, behind small pluggable interfaces.

`console` senders write the message to the server log and keep the last few in memory (tests read
them). They are for development; production configures `twilio` / `smtp`. A code is never returned in
an HTTP response, whatever the sender.
"""

from __future__ import annotations

import logging
import smtplib
from collections import deque
from dataclasses import dataclass
from email.message import EmailMessage
from functools import cache
from typing import Protocol

import httpx

from cura.api.settings import settings

log = logging.getLogger("cura.messaging")


@dataclass
class Sent:
    channel: str  # sms | voice | email
    to: str
    text: str
    code: str | None = None


class SmsSender(Protocol):
    def send_sms(self, to: str, text: str, *, code: str | None = None) -> None: ...
    def call_with_code(self, to: str, code: str) -> None: ...


class EmailSender(Protocol):
    def send_email(self, to: str, subject: str, text: str, *, code: str | None = None) -> None: ...


class ConsoleSender:
    """Development only: logs the message. Keeps the last 50 in `outbox` for tests."""

    def __init__(self) -> None:
        self.outbox: deque[Sent] = deque(maxlen=50)

    def send_sms(self, to: str, text: str, *, code: str | None = None) -> None:
        self.outbox.append(Sent("sms", to, text, code))
        log.warning("[dev sms] to %s: %s", to, text)

    def call_with_code(self, to: str, code: str) -> None:
        self.outbox.append(Sent("voice", to, f"voice call reading code {code}", code))
        log.warning("[dev voice call] to %s: code %s", to, code)

    def send_email(self, to: str, subject: str, text: str, *, code: str | None = None) -> None:
        self.outbox.append(Sent("email", to, f"{subject}\n{text}", code))
        log.warning("[dev email] to %s: %s / %s", to, subject, text)

    def last(self, channel: str | None = None, to: str | None = None) -> Sent | None:
        for item in reversed(self.outbox):
            if (channel is None or item.channel == channel) and (to is None or item.to == to):
                return item
        return None


class TwilioSender:
    """SMS through the Messages API, voice through the Calls API reading the code twice."""

    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/{resource}.json"

    def __init__(self, sid: str, token: str, sender: str) -> None:
        self.sid, self.token, self.sender = sid, token, sender

    def _post(self, resource: str, data: dict) -> None:
        r = httpx.post(
            self.API.format(sid=self.sid, resource=resource), data=data, auth=(self.sid, self.token), timeout=10
        )
        if r.status_code >= 300:
            log.error("twilio %s failed: %s", resource, r.status_code)
            raise RuntimeError("SMS provider rejected the message")

    def send_sms(self, to: str, text: str, *, code: str | None = None) -> None:
        self._post("Messages", {"To": to, "From": self.sender, "Body": text})

    def call_with_code(self, to: str, code: str) -> None:
        spoken = ", ".join(code)
        twiml = f"<Response><Say>Your Cura code is {spoken}. Again: {spoken}.</Say></Response>"
        self._post("Calls", {"To": to, "From": self.sender, "Twiml": twiml})


class SmtpSender:
    def __init__(self, host: str, port: int, user: str | None, password: str | None, sender: str) -> None:
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender

    def send_email(self, to: str, subject: str, text: str, *, code: str | None = None) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(text)
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password or "")
            smtp.send_message(msg)


_console = ConsoleSender()


@cache
def sms() -> SmsSender:
    s = settings()
    if s.sms_provider == "twilio":
        if not (s.twilio_account_sid and s.twilio_auth_token and s.twilio_from):
            raise RuntimeError("CURA_TWILIO_ACCOUNT_SID, CURA_TWILIO_AUTH_TOKEN and CURA_TWILIO_FROM are required")
        return TwilioSender(s.twilio_account_sid, s.twilio_auth_token, s.twilio_from)
    if s.is_production:
        log.error("CURA_SMS_PROVIDER=console in production: codes are only written to the log")
    return _console


@cache
def email() -> EmailSender:
    s = settings()
    if s.email_provider == "smtp":
        if not s.smtp_host:
            raise RuntimeError("CURA_SMTP_HOST is required")
        return SmtpSender(s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password, s.smtp_from)
    return _console


def console() -> ConsoleSender:
    return _console

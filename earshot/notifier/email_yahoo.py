"""Yahoo SMTP notifier.

Sends multipart (plain text + HTML) email via ``smtp.mail.yahoo.com:465`` over
SSL. Uses the user's Yahoo address + app password (NOT the login password).
App password is generated at https://login.yahoo.com → Account Security →
"Generate app password".

On transient failures (network blip, SMTP timeout), the caller leaves the
items un-notified so a later run can retry. The notifier records the error
in ``AlertResult.error`` and lets the caller decide what to do.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from earshot.digest import DigestPayload, render_html, render_markdown
from earshot.notifier.base import AlertResult


log = logging.getLogger("earshot.notifier.email_yahoo")

SMTP_HOST = "smtp.mail.yahoo.com"
SMTP_PORT = 465  # SSL


class EmailYahooNotifier:
    channel = "email"

    def __init__(self, sender_email: str, app_password: str, recipient: str) -> None:
        self._email = sender_email
        # Yahoo presents the 16-char app password with spaces in the UI for
        # readability. SMTP accepts it either way, but strip just in case the
        # user copy-pasted with spaces.
        self._password = (app_password or "").replace(" ", "")
        self._recipient = recipient

    def send(self, payload: DigestPayload) -> AlertResult:
        msg = self._build_message(payload)
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(self._email, self._password)
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            err = f"SMTP auth failed (check YAHOO_APP_PASSWORD): {e}"
            log.warning("%s", err)
            return AlertResult(status="failed", channel=self.channel,
                               recipient=self._recipient, error=err)
        except (smtplib.SMTPException, OSError, TimeoutError) as e:
            err = f"SMTP send failed: {e}"
            log.warning("%s", err)
            return AlertResult(status="failed", channel=self.channel,
                               recipient=self._recipient, error=err)
        return AlertResult(status="sent", channel=self.channel,
                           recipient=self._recipient)

    def send_test(self, subject: str = "Earshot test email", body: str = "If you see this, Earshot can send mail.") -> AlertResult:
        """Minimal credential test path — no DigestPayload needed."""
        msg = EmailMessage()
        self._apply_standard_headers(msg, subject)
        msg.set_content(body)
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(self._email, self._password)
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            return AlertResult(status="failed", channel=self.channel,
                               recipient=self._recipient, error=f"auth: {e}")
        except (smtplib.SMTPException, OSError, TimeoutError) as e:
            return AlertResult(status="failed", channel=self.channel,
                               recipient=self._recipient, error=str(e))
        return AlertResult(status="sent", channel=self.channel,
                           recipient=self._recipient)

    def _build_message(self, payload: DigestPayload) -> EmailMessage:
        msg = EmailMessage()
        self._apply_standard_headers(msg, payload.subject)
        msg.set_content(render_markdown(payload))
        msg.add_alternative(render_html(payload), subtype="html")
        return msg

    def _apply_standard_headers(self, msg: EmailMessage, subject: str) -> None:
        """Headers Yahoo expects from a 'real' mail client.

        Without explicit Date / Message-ID / friendly From name, Yahoo silently
        drops self-to-self mail that contains HTML and outbound links — even
        when SMTP returns success. Adding these makes the message look like
        it came from a normal MUA.
        """
        domain = self._email.split("@", 1)[-1] or "earshot"
        msg["From"] = formataddr(("Earshot", self._email))
        msg["To"] = self._recipient
        msg["Reply-To"] = self._email
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=domain)
        msg["X-Mailer"] = "Earshot/0.1"

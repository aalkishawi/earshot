"""`earshot doctor` — green/red diagnostic across every configured service.

Each check is independent and returns a (status, message) pair. The CLI
formats them with colored prefixes. Designed to be safe to run repeatedly:
no DB writes, no outbound calls, no emails sent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from earshot.config import Config


@dataclass
class CheckResult:
    name: str
    status: str          # 'ok' | 'warn' | 'fail' | 'skip'
    message: str
    hint: str = ""       # one-liner that tells the user what to do if not ok


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #

def check_env_file(cfg: Config) -> CheckResult:
    env_path = cfg.db_path.parent / ".env"
    # config.REPO_ROOT/.env is the canonical path; check that one specifically.
    from earshot import config as config_mod
    real_env = config_mod.REPO_ROOT / ".env"
    if real_env.exists():
        return CheckResult("env file", "ok", f"found at {real_env}")
    return CheckResult(
        "env file", "fail", f"missing at {real_env}",
        hint="run `earshot init` to create it",
    )


def check_db(cfg: Config) -> CheckResult:
    if cfg.db_path.exists():
        return CheckResult("database", "ok", f"found at {cfg.db_path}")
    return CheckResult(
        "database", "fail", f"missing at {cfg.db_path}",
        hint="run `earshot init-db`",
    )


def check_channels(cfg: Config) -> CheckResult:
    if not cfg.channels_path.exists():
        return CheckResult(
            "channels.yaml", "fail", "missing",
            hint="run `earshot init` (the wizard will add at least one channel)",
        )
    if not cfg.channels:
        return CheckResult(
            "channels", "warn", "channels.yaml exists but is empty",
            hint="add a channel via `earshot resolve-channel @handle`",
        )
    return CheckResult("channels", "ok", f"{len(cfg.channels)} configured")


def check_anthropic(cfg: Config) -> CheckResult:
    if not cfg.anthropic_api_key:
        return CheckResult(
            "anthropic", "fail", "ANTHROPIC_API_KEY not set",
            hint="set in .env (console.anthropic.com → API keys)",
        )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with the single word 'ok'."}],
        )
        if any(getattr(b, "text", "").strip() for b in resp.content):
            return CheckResult("anthropic", "ok", "Claude API call succeeded")
        return CheckResult("anthropic", "warn", "Claude returned empty response")
    except Exception as e:
        msg = str(e)
        hint = "check the API key and account credit balance"
        if "credit balance" in msg.lower():
            hint = "buy credits at console.anthropic.com → Plans & Billing"
        return CheckResult("anthropic", "fail", f"{type(e).__name__}: {msg[:160]}", hint=hint)


def check_yahoo(cfg: Config) -> CheckResult:
    if not cfg.is_complete_for_email():
        missing = cfg.missing_keys_for_email()
        return CheckResult(
            "yahoo smtp", "fail", f"missing: {', '.join(missing)}",
            hint="run `earshot init` and set up the email section",
        )
    try:
        import smtplib
        with smtplib.SMTP_SSL("smtp.mail.yahoo.com", 465, timeout=15) as s:
            s.login(cfg.yahoo_email or "", (cfg.yahoo_app_password or "").replace(" ", ""))
        return CheckResult("yahoo smtp", "ok", f"login OK as {cfg.yahoo_email}")
    except Exception as e:
        return CheckResult(
            "yahoo smtp", "fail", f"{type(e).__name__}: {str(e)[:160]}",
            hint="generate a fresh app password at login.yahoo.com → Account Security",
        )


def check_groq(cfg: Config) -> CheckResult:
    if not cfg.groq_api_key:
        return CheckResult(
            "groq (asr)", "skip", "not configured (optional fallback)",
        )
    try:
        import requests
        r = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {cfg.groq_api_key}"}, timeout=10,
        )
        if r.status_code == 200:
            return CheckResult("groq (asr)", "ok", "API key works")
        return CheckResult(
            "groq (asr)", "fail", f"HTTP {r.status_code}",
            hint="regenerate at console.groq.com → API keys",
        )
    except Exception as e:
        return CheckResult("groq (asr)", "fail", f"{type(e).__name__}: {str(e)[:160]}")


def check_twilio(cfg: Config) -> CheckResult:
    if not cfg.is_complete_for_voice():
        missing = cfg.missing_keys_for_voice()
        # Voice is optional; only warn if some keys are set (partial config).
        any_set = any([
            cfg.twilio_account_sid, cfg.twilio_auth_token,
            cfg.twilio_from_number, cfg.twilio_to_number,
        ])
        if not any_set:
            return CheckResult("twilio voice", "skip", "not configured (optional)")
        return CheckResult(
            "twilio voice", "warn", f"partial config; missing: {', '.join(missing)}",
            hint="run `earshot init` to finish the voice section",
        )
    try:
        from twilio.rest import Client
        client = Client(cfg.twilio_account_sid or "", cfg.twilio_auth_token or "")
        acct = client.api.v2010.accounts(cfg.twilio_account_sid).fetch()
        return CheckResult("twilio voice", "ok", f"{acct.friendly_name} ({acct.status})")
    except Exception as e:
        return CheckResult(
            "twilio voice", "fail", f"{type(e).__name__}: {str(e)[:160]}",
            hint="check creds at console.twilio.com",
        )


def check_data_dir(cfg: Config) -> CheckResult:
    if cfg.data_dir.exists():
        return CheckResult("data dir", "ok", f"{cfg.data_dir}")
    return CheckResult(
        "data dir", "warn", f"{cfg.data_dir} (will be created on first run)",
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

CHECKS: list[Callable[[Config], CheckResult]] = [
    check_env_file,
    check_db,
    check_data_dir,
    check_channels,
    check_anthropic,
    check_yahoo,
    check_groq,
    check_twilio,
]


def run_all(cfg: Config) -> list[CheckResult]:
    return [chk(cfg) for chk in CHECKS]

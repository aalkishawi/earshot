"""Interactive-call webhook service (v2).

A small FastAPI app Twilio calls back into to drive the conversation. See
``app.py`` for endpoints.
"""
from earshot.webhook.app import create_app

__all__ = ["create_app"]

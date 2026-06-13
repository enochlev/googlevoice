"""
Data wrappers and exceptions for the modern Google Voice API.

The legacy API returned XML wrapping JSON wrapping HTML; the modern API returns
plain JSON, so the old ``XMLParser`` machinery is gone.  These light wrappers
give attribute access over the JSON the ``voiceclient`` endpoints return.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class LoginError(Exception):
    """The session is missing, rejected, or expired."""


class APIError(Exception):
    """A voiceclient API call returned a non-200 response."""


class DownloadError(Exception):
    """A voicemail/recording could not be downloaded."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ms_to_datetime(ms: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Messages and threads
# --------------------------------------------------------------------------- #
class Message:
    """A single message (SMS, voicemail, or call) within a thread."""

    def __init__(self, data: dict):
        self._data = data

    @property
    def id(self) -> str | None:
        return self._data.get('id')

    @property
    def text(self) -> str | None:
        """The SMS body, or voicemail transcript."""
        return self._data.get('messageText')

    @property
    def type(self) -> str | None:
        """e.g. ``smsIn``, ``smsOut``, ``voicemail``, ``missed``."""
        return self._data.get('type')

    @property
    def incoming(self) -> bool:
        return (self.type or '').lower().endswith('in')

    @property
    def phone_number(self) -> str | None:
        """The other party's number."""
        return (self._data.get('contact') or {}).get('phoneNumber')

    @property
    def did(self) -> str | None:
        """Your Google Voice number this message went through."""
        return self._data.get('did')

    @property
    def start_time(self) -> datetime | None:
        return _ms_to_datetime(self._data.get('startTime'))

    def as_dict(self) -> dict:
        """A plain, JSON-serializable view of this message."""
        when = self.start_time
        return {
            'id': self.id,
            'text': self.text,
            'type': self.type,
            'incoming': self.incoming,
            'phone_number': self.phone_number,
            'did': self.did,
            'start_time': when.isoformat() if when else None,
        }

    def __repr__(self) -> str:
        arrow = '<-' if self.incoming else '->'
        return f'<Message {arrow} {self.phone_number}: {self.text!r}>'


class Thread:
    """A conversation: a list of :class:`Message` plus reply helpers."""

    def __init__(self, voice, data: dict):
        self.voice = voice
        self._data = data

    @property
    def id(self) -> str | None:
        """e.g. ``t.+12085551234`` (number), ``t.22000`` (short code)."""
        return self._data.get('id')

    @property
    def read(self) -> bool:
        return bool(self._data.get('read'))

    @property
    def is_text(self) -> bool:
        return bool(self._data.get('isText'))

    @property
    def messages(self) -> list[Message]:
        """Messages, newest first (as Google returns them)."""
        return [Message(m) for m in self._data.get('item', [])]

    @property
    def latest(self) -> Message | None:
        items = self._data.get('item') or []
        return Message(items[0]) if items else None

    @property
    def latest_text(self) -> str | None:
        return self.latest.text if self.latest else None

    @property
    def contact(self) -> str | None:
        """The recipient's phone number / identifier for this thread."""
        keys = self._data.get('headingContactsPhoneNumberKey') or []
        if keys:
            return keys[0]
        latest = self.latest
        return latest.phone_number if latest else None

    def reply(self, text: str) -> dict:
        """Send ``text`` back into this conversation."""
        return self.voice.send_sms(self.contact, text, thread_id=self.id)

    def as_dict(self) -> dict:
        """A plain, JSON-serializable view of this conversation."""
        return {
            'id': self.id,
            'contact': self.contact,
            'read': self.read,
            'is_text': self.is_text,
            'messages': [m.as_dict() for m in self.messages],
        }

    def __repr__(self) -> str:
        return f'<Thread {self.id} ({self.contact}): {self.latest_text!r}>'

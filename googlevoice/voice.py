"""
Client for the modern Google Voice web API.

The legacy ``/voice/b/0/`` HTML/XML endpoints this library was built on are
gone (see issues #7 and #8).  This is a ground-up reimplementation against the
JSON API the current voice.google.com web app uses:

    https://clients6.google.com/voice/v1/voiceclient/...

Authentication is handled by :mod:`googlevoice.auth` (a portable cookie
session, no browser needed at call time).  Typical use::

    from googlevoice import Voice
    v = Voice()                       # loads ~/.googlevoice/session.json
    print(v.number)                   # your Google Voice number
    for thread in v.threads():        # recent SMS/voicemail threads
        print(thread.contact, thread.latest_text)
    v.send_sms('+15555551234', 'Hello from Python!')

Run ``python -m googlevoice.auth login`` once first to create the session.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from . import util
from .auth import API_BASE, API_KEY, DEFAULT_SESSION_PATH, Credentials

log = logging.getLogger(__name__)

# Country code assumed for bare national numbers (e.g. ``2085551234``). Change
# this for non-US accounts.
DEFAULT_COUNTRY_CODE = '+1'


def normalize_number(number: str) -> str:
    """
    Normalize a phone number for use as a thread id / recipient.

    Strips common formatting (spaces, parens, dashes, dots) and, for bare
    national numbers, prepends :data:`DEFAULT_COUNTRY_CODE`. Already-E.164
    numbers, thread ids (``t.``/``g.``), and short codes are passed through.

    >>> normalize_number('+1 (208) 555-0123')
    '+12085550123'
    >>> normalize_number('2085550123')
    '+12085550123'
    >>> normalize_number('22000')
    '22000'
    """
    n = number.strip()
    if n.startswith(('t.', 'g.')):
        return n
    n = re.sub(r'[\s()\-.]', '', n)
    if n.startswith('+'):
        return n
    digits = n.lstrip('+')
    if len(digits) <= 6:  # short code (e.g. 22000) -- not a dialable number
        return n
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    if len(digits) == 10:
        return DEFAULT_COUNTRY_CODE + digits
    return n


# api2thread/list folder selector (first element of the request body).
class Folder:
    ALL = 1
    INBOX = 2  # what the web app loads by default; SMS + voicemail conversations


class Voice:
    """
    Main entry point for the modern Google Voice API.

    Pass :class:`~googlevoice.auth.Credentials`, or leave it ``None`` to load
    the default saved session (``~/.googlevoice/session.json``).
    """

    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        session_path=DEFAULT_SESSION_PATH,
    ):
        self.credentials = credentials or Credentials.load(session_path)
        self._http = self.credentials.requests_session()

    # ----------------------------------------------------------------- #
    # Low-level call
    # ----------------------------------------------------------------- #
    def _call(self, endpoint: str, body: Any, *, alt: str = 'json') -> Any:
        """POST ``body`` (a JSON-able protojson array) to ``endpoint``."""
        resp = self._http.post(
            API_BASE + endpoint,
            params={'alt': alt, 'key': API_KEY},
            headers=self.credentials.auth_headers(),
            data=json.dumps(body),
            timeout=30,
        )
        if resp.status_code == 401:
            raise util.LoginError(
                'Google rejected the session (401). It may be expired -- '
                're-run `python -m googlevoice.auth login`.'
            )
        if resp.status_code != 200:
            raise util.APIError(
                f'{endpoint} -> HTTP {resp.status_code}: {resp.text[:300]}'
            )
        if alt == 'json':
            try:
                return resp.json()
            except ValueError as err:
                raise util.APIError(f'{endpoint}: bad JSON response') from err
        return resp.text

    # ----------------------------------------------------------------- #
    # Account
    # ----------------------------------------------------------------- #
    def account(self) -> dict:
        """Raw account info (primary number, phones, settings)."""
        return self._call('account/get', [None, 1])['account']

    @property
    def number(self) -> str | None:
        """Your Google Voice number in E.164 form, e.g. ``+12085551234``."""
        return self.account().get('primaryDid')

    # ----------------------------------------------------------------- #
    # Reading conversations
    # ----------------------------------------------------------------- #
    def threads(
        self,
        folder: int = Folder.INBOX,
        count: int = 20,
        *,
        messages: int = 15,
        cursor: str | None = None,
    ) -> list[util.Thread]:
        """
        Return recent conversation :class:`~googlevoice.util.Thread` objects.

        ``count`` is how many conversations to return; ``messages`` is how many
        recent messages to include per conversation. ``cursor`` is the
        ``startTime`` of the oldest thread from a previous page (for paging).
        """
        body = [folder, count, messages, cursor, None, [None, 1, 1, 1]]
        data = self._call('api2thread/list', body)
        return [util.Thread(self, t) for t in data.get('thread', [])]

    def inbox(self, count: int = 20) -> list[util.Thread]:
        """Convenience: the default inbox conversations."""
        return self.threads(Folder.INBOX, count)

    def thread(
        self, recipient: str, *, messages: int = 50, search: int = 50
    ) -> util.Thread | None:
        """
        Return the conversation with ``recipient`` (E.164 or a number this
        library can normalize, e.g. ``2085551234``), with up to ``messages``
        recent messages, or ``None`` if it is not among the ``search`` most
        recent conversations.
        """
        tid = _thread_id_for(recipient)
        for thread in self.threads(count=search, messages=messages):
            if thread.id == tid:
                return thread
        return None

    # ----------------------------------------------------------------- #
    # Sending
    # ----------------------------------------------------------------- #
    def send_sms(
        self,
        recipient: str,
        text: str,
        *,
        thread_id: str | None = None,
        recaptcha: list | None = None,
    ) -> dict:
        """
        Send an SMS ``text`` to ``recipient`` (E.164, e.g. ``+12085551234``).

        If ``thread_id`` is given the message is sent into that existing
        conversation; otherwise it is derived from ``recipient`` (Google Voice
        thread ids for a number are simply ``t.<E.164>``).

        .. important::
           Unlike reads, Google gates *sending* behind reCAPTCHA / BotGuard
           anti-abuse tokens (the final element of the request body), which can
           only be produced by executing Google's JavaScript in a browser.  For
           normal use, prefer :class:`googlevoice.browser.BrowserSender`, which
           drives a browser to mint those tokens and send for you.  This
           low-level method only sends if you pass a pre-minted ``recaptcha``
           payload (``[token, None, None, token2]``); without it Google returns
           ``429 RESOURCE_EXHAUSTED``.
        """
        tid = thread_id or _thread_id_for(recipient)
        nonce = int(time.time() * 1000)
        # protojson body for api2thread/sendsms, reverse-engineered from the
        # live web app: four leading nulls, text, thread id, two nulls,
        # [nonce], null, then the reCAPTCHA/anti-abuse token payload.
        body = [None, None, None, None, text, tid, None, None, [nonce], None, recaptcha]
        return self._call('api2thread/sendsms', body)


def _thread_id_for(recipient: str) -> str:
    """Map a recipient to a thread id (``t.<E.164>``); pass ids through."""
    normalized = normalize_number(recipient)
    if normalized.startswith(('t.', 'g.')):
        return normalized
    return 't.' + normalized

"""
Browser-driven SMS sending for Google Voice.

Reading from Google Voice is a plain HTTP call (see :mod:`googlevoice.voice`),
but *sending* is gated behind anti-abuse tokens -- a reCAPTCHA Enterprise token
and a BotGuard token -- that can only be produced by executing Google's
obfuscated JavaScript in a real browser. Rather than try to forge those
(infeasible) or pay a captcha service (which can't generate BotGuard tokens and
would score as a bot anyway), :class:`BrowserSender` simply drives the real
``voice.google.com`` web app via ``nodriver``: the page mints valid tokens
itself, bound to your live session.

Usage::

    from googlevoice.browser import BrowserSender

    with BrowserSender() as sender:           # reuses ~/.googlevoice/chrome-profile
        sender.send_sms('+12085551234', 'Hello from Python!')

The profile must already be signed in -- run ``python -m googlevoice.auth
login`` once first. Sending therefore needs Chrome installed and running;
reading does not.
"""

from __future__ import annotations

import logging

from .auth import DEFAULT_PROFILE_DIR, ORIGIN
from .util import APIError, LoginError
from .voice import normalize_number

log = logging.getLogger(__name__)

# A JS helper namespace installed into the page. Google Voice buries its
# controls in nested Shadow DOM, so every lookup recurses through shadow roots
# rather than use a flat CSS selector.
_HELPERS_JS = r"""
window.__gv = (function () {
  function deep(sel, root, acc) {
    root = root || document; acc = acc || [];
    root.querySelectorAll(sel).forEach(e => acc.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deep(sel, e.shadowRoot, acc); });
    return acc;
  }
  const vis = e => e && e.offsetParent !== null && !e.disabled;
  return {
    clickAria(label) {
      const el = deep('[aria-label]').find(e => vis(e) && e.getAttribute('aria-label') === label);
      if (!el) return 'MISS'; el.click(); return 'OK';
    },
    clickClass(cls) {
      const el = deep('.' + cls).find(vis);
      if (!el) return 'MISS'; el.click(); return 'OK';
    },
    focus(placeholder) {
      const el = deep('input, textarea').find(
        e => vis(e) && (e.placeholder || '').includes(placeholder));
      if (!el) return 'MISS';
      el.scrollIntoView(); el.click(); el.focus(); return 'OK';
    },
  };
})(); 'installed'
"""


class BrowserSender:
    """
    Sends Google Voice SMS by driving the real web app in a Chrome instance
    (via ``nodriver``). Reuses the signed-in profile created by
    ``python -m googlevoice.auth login``.
    """

    def __init__(
        self,
        profile_dir=DEFAULT_PROFILE_DIR,
        *,
        headless: bool = False,
        timeout: float = 60,
    ):
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout = timeout
        self._browser = None
        self._tab = None
        self._loop = None
        self._statuses: list[int] = []

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def __enter__(self) -> BrowserSender:  # noqa: PYI034
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        import nodriver as uc

        self._loop = uc.loop()
        self._loop.run_until_complete(self._start())

    async def _start(self) -> None:
        import nodriver as uc
        from nodriver import cdp

        self._browser = await uc.start(
            headless=self.headless,
            user_data_dir=str(self.profile_dir),
            browser_args=['--no-first-run', '--no-default-browser-check'],
        )
        self._tab = await self._browser.get(ORIGIN)

        def _on_response(ev):
            if 'sendsms' in ev.response.url:
                self._statuses.append(int(ev.response.status))

        self._tab.add_handler(cdp.network.ResponseReceived, _on_response)
        await self._tab.send(cdp.network.enable())
        await self._sleep(4)

        url = await self._tab.evaluate('location.href', await_promise=False)
        if 'voice.google.com' not in (url or '') or 'workspace.google.com' in (
            url or ''
        ):
            raise LoginError(
                'Browser profile is not signed in to Google Voice. '
                'Run `python -m googlevoice.auth login` first.'
            )

    def close(self) -> None:
        if self._browser is not None:
            self._browser.stop()
            self._browser = self._tab = None

    # ------------------------------------------------------------------ #
    # sending
    # ------------------------------------------------------------------ #
    def send_sms(
        self, recipient: str, text: str, *, thread_id: str | None = None
    ) -> None:
        """
        Send ``text`` to ``recipient`` (E.164, e.g. ``+12085551234``) by driving
        the web app's "new message" composer, which routes correctly for both
        new and existing conversations. ``thread_id`` is accepted for API
        symmetry with :meth:`googlevoice.Voice.send_sms` but is not needed here.
        """
        if self._browser is None:
            raise RuntimeError(
                'BrowserSender not started; use it as a context manager.'
            )
        self._loop.run_until_complete(self._send(normalize_number(recipient), text))

    async def _type(self, text: str) -> None:
        """Type ``text`` into the focused element with real (trusted) keystrokes."""
        from nodriver import cdp

        for ch in text:
            await self._tab.send(
                cdp.input_.dispatch_key_event(type_='keyDown', text=ch, key=ch)
            )
            await self._tab.send(cdp.input_.dispatch_key_event(type_='keyUp', key=ch))
            await self._sleep(0.04)

    async def _press_enter(self) -> None:
        """Press Enter on the focused element (sends the message)."""
        from nodriver import cdp

        for kind in ('keyDown', 'keyUp'):
            await self._tab.send(
                cdp.input_.dispatch_key_event(
                    type_=kind,
                    key='Enter',
                    code='Enter',
                    windows_virtual_key_code=13,
                    native_virtual_key_code=13,
                )
            )

    async def _action(self, expr: str):
        """Evaluate ``expr``; reinstall the JS helpers and retry once if needed."""
        try:
            return await self._tab.evaluate(expr, await_promise=False)
        except Exception:
            await self._tab.evaluate(_HELPERS_JS, await_promise=False)
            return await self._tab.evaluate(expr, await_promise=False)

    async def _poll(self, expr: str, *, tries: int = 20, delay: float = 0.5) -> str:
        """Call ``expr`` until it returns ``'OK'`` or attempts run out."""
        res = 'MISS'
        for _ in range(tries):
            res = await self._action(expr)
            if res == 'OK':
                return res
            await self._sleep(delay)
        return res

    async def _send(self, recipient: str, text: str) -> None:
        before = len(self._statuses)
        await self._browser.get(f'{ORIGIN}/u/0/messages')
        await self._sleep(5)
        await self._tab.evaluate(_HELPERS_JS, await_promise=False)

        # 1. open the "new message" composer
        if await self._poll("window.__gv.clickAria('Send new message')") != 'OK':
            raise APIError('Could not open the new-message composer.')
        await self._sleep(1)

        # 2. focus the recipient field and type with REAL keystrokes -- the
        #    Material autocomplete only opens its "Send to <number>" suggestion
        #    in response to genuine key events, not a synthetic value set.
        if await self._poll("window.__gv.focus('Type a name or phone number')") != 'OK':
            raise APIError('Could not find the recipient field.')
        await self._type(recipient)
        await self._sleep(1.5)  # let the autocomplete populate
        # 3. pick the "Send to <number>" suggestion
        if await self._poll("window.__gv.clickClass('send-to-label')") != 'OK':
            raise APIError(
                f'No "Send to {recipient}" suggestion appeared; '
                'is the number valid and textable?'
            )
        await self._sleep(1)

        # 4. focus the compose box, type the message with real keystrokes, and
        #    press Enter to send (the new-message composer sends on Enter).
        if await self._poll("window.__gv.focus('Type a message')") != 'OK':
            raise APIError('Could not find the compose box.')
        await self._type(text)
        await self._sleep(0.5)
        await self._press_enter()

        # 5. confirm the send actually went through
        for _ in range(int(self.timeout * 2)):
            await self._sleep(0.5)
            if len(self._statuses) > before:
                status = self._statuses[-1]
                if status == 200:
                    log.info('sent to %s', recipient)
                    return
                raise APIError(f'sendsms returned HTTP {status}')
        raise APIError('Timed out waiting for the message to send.')

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

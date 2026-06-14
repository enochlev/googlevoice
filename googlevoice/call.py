"""
Browser-driven *calling* for Google Voice.

Like sending an SMS (see :mod:`googlevoice.browser`), placing a call cannot be
done with a plain HTTP request: Google Voice calls go over **WebRTC** inside the
web app, so the audio path lives in the browser. :class:`Caller` therefore
drives the real ``voice.google.com`` web app via ``nodriver`` to place the call.

The audio the other party hears is whatever Chrome captures as the
*microphone*. To play a file into a call we hand Chrome a WAV file as a fake
microphone using its built-in flags (no system audio setup required)::

    --use-fake-ui-for-media-stream         # auto-grant the mic permission
    --use-fake-device-for-media-stream
    --use-file-for-fake-audio-capture=FILE.wav%noloop

Usage::

    from googlevoice.call import Caller

    with Caller(audio_wav='test.wav') as caller:   # reuses ~/.googlevoice/chrome-profile
        caller.place_call('+12085550123')

The profile must already be signed in -- run ``python -m googlevoice.auth
login`` once first. A later phase swaps the static WAV for a live virtual
microphone (e.g. to bridge a realtime voice model into the call).
"""

from __future__ import annotations

import logging
import re

from ._browserlock import ProfileLock
from .auth import DEFAULT_PROFILE_DIR, ORIGIN
from .browser import _HELPERS_JS
from .util import APIError, LoginError
from .voice import normalize_number

log = logging.getLogger(__name__)

# Extends the shared ``window.__gv`` helper namespace (defined in
# :mod:`googlevoice.browser`) with calling-specific lookups. Google Voice buries
# its controls in nested Shadow DOM, so every lookup recurses shadow roots.
_CALL_HELPERS_JS = r"""
(function () {
  function deep(sel, root, acc) {
    root = root || document; acc = acc || [];
    root.querySelectorAll(sel).forEach(e => acc.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deep(sel, e.shadowRoot, acc); });
    return acc;
  }
  const vis = e => e && e.offsetParent !== null && !e.disabled;
  window.__gv = window.__gv || {};
  // Click the first visible element whose aria-label *contains* a substring.
  window.__gv.clickAriaLike = function (sub) {
    sub = sub.toLowerCase();
    const el = deep('[aria-label]').find(
      e => vis(e) && (e.getAttribute('aria-label') || '').toLowerCase().includes(sub));
    if (!el) return 'MISS'; el.click(); return 'OK';
  };
  // The call panel's class + collapsed text, for call-state detection.
  // (idle: class has "no-active-call"; ringing: text has "Calling…";
  //  connected: text has "Elapsed time"/a "00:0X" timer; ended: "Call ended".)
  window.__gv.callPanel = function () {
    const p = deep('[aria-label="Call panel"]')[0];
    return JSON.stringify({
      present: !!p,
      cls: p ? p.className : '',
      txt: p ? (p.textContent || '').replace(/\s+/g, ' ').trim() : '',
    });
  };
  // Dump visible interactive controls, for selector discovery / debugging.
  window.__gv.dump = function () {
    return JSON.stringify(deep('button, a, input, textarea, [role="button"], [aria-label]')
      .filter(vis)
      .map(e => ({
        tag: e.tagName.toLowerCase(),
        aria: e.getAttribute('aria-label') || '',
        ph: e.placeholder || '',
        cls: (typeof e.className === 'string' ? e.className : '') || '',
        text: (e.textContent || '').trim().slice(0, 40),
      }))
      .filter(o => o.aria || o.ph || o.text));
  };
  return 'call-helpers-installed';
})();
"""


class Caller:
    """
    Places Google Voice calls by driving the real web app in a Chrome instance
    (via ``nodriver``). Reuses the signed-in profile created by
    ``python -m googlevoice.auth login``.

    :class:`Caller` only dials and tracks call state. Call audio is layered on
    top via the ``on_connected`` hook of :meth:`place_call`, which streams audio
    through PipeWire only after the callee answers -- see
    :mod:`googlevoice.playback` (TTS / file) and :mod:`googlevoice.realtime`
    (live agent). ``extra_browser_args`` / ``init_script`` let those callers set
    up real-device audio capture (e.g. auto-grant the mic, disable WebRTC AGC).
    """

    def __init__(
        self,
        profile_dir=DEFAULT_PROFILE_DIR,
        *,
        extra_browser_args: list[str] | None = None,
        init_script: str | None = None,
        headless: bool = False,
        timeout: float = 120,
        wait: bool = False,
    ):
        self.profile_dir = profile_dir
        # Extra Chrome flags (e.g. '--use-fake-ui-for-media-stream' to auto-grant
        # the mic with real devices for live audio routing).
        self.extra_browser_args = list(extra_browser_args or [])
        # JS run at the start of every document (used to disable WebRTC mic
        # processing, which otherwise mangles injected audio).
        self.init_script = init_script
        self.headless = headless
        self.timeout = timeout
        # wait=False -> raise BrowserBusyError if the profile is busy;
        # wait=True -> queue until it frees up.
        self.wait = wait
        self._browser = None
        self._tab = None
        self._loop = None
        self._lock = None
        self._call_events: list[str] = []

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def __enter__(self) -> Caller:  # noqa: PYI034
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        import nodriver as uc
        # One Chrome per profile: take the lock (clearing any stale one) first.
        self._lock = ProfileLock(self.profile_dir, wait=self.wait)
        self._lock.acquire()
        try:
            self._loop = uc.loop()
            self._loop.run_until_complete(self._start())
        except BaseException:
            self.close()
            raise

    async def _start(self) -> None:
        import nodriver as uc
        from nodriver import cdp

        args = ['--no-first-run', '--no-default-browser-check', *self.extra_browser_args]
        self._browser = await uc.start(
            headless=self.headless,
            user_data_dir=str(self.profile_dir),
            browser_args=args,
        )

        # Register the init script on the initial tab BEFORE loading any Google
        # Voice page, so it's in place before the page calls getUserMedia.
        if self.init_script:
            init_tab = self._browser.main_tab or await self._browser.get('about:blank')
            await init_tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=self.init_script)
            )

        # Go straight to the dialer (one navigation -- faster than landing on
        # the messages page first).
        self._tab = await self._browser.get(f'{ORIGIN}/u/0/calls')

        # Track the call-setup RPCs so we can tell a placed call from a no-op.
        def _on_response(ev):
            url = ev.response.url
            if any(k in url for k in ('call/create', 'voiceclient/call', 'placeCall')):
                self._call_events.append(f'{ev.response.status} {url}')

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
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    # ------------------------------------------------------------------ #
    # calling
    # ------------------------------------------------------------------ #
    def place_call(
        self,
        recipient: str,
        *,
        hold: float | None = None,
        wait_for_answer: bool = True,
        ring_timeout: float = 45.0,
        on_connected=None,
    ) -> str:
        """
        Place a call to ``recipient`` (E.164, e.g. ``+12085550123``) and return
        the outcome.

        If ``on_connected`` (an ``async def f(caller)``) is given, it is awaited
        once the line connects instead of the fixed ``hold`` -- it owns the call
        and returns when done; the call is then hung up. This is how the realtime
        voice bridge drives a live conversation.

        With ``wait_for_answer`` (default), waits through ringing until the line
        connects, then stays on for ``hold`` seconds (default: the WAV duration,
        or 20s) so the fake-mic audio plays, hanging up early if the other side
        ends the call. If nobody picks up within ``ring_timeout`` seconds, hangs
        up without holding.

        Returns one of: ``'completed'`` (connected and we hung up),
        ``'remote-ended'`` (the other side hung up during the call),
        ``'no-answer'`` (ring timed out), or ``'declined'`` (ended before
        connecting).

        Note: Google Voice *voicemail* answers the call, so it registers as
        ``connected`` here just like a person would -- the DOM cannot tell them
        apart. Distinguishing a live human from a voicemail greeting needs
        audio-level detection (e.g. the realtime model in the Phase 2 bridge
        listening for the greeting).
        """
        if self._browser is None:
            raise RuntimeError('Caller not started; use it as a context manager.')
        return self._loop.run_until_complete(
            self._place_call(
                normalize_number(recipient),
                hold,
                wait_for_answer,
                ring_timeout,
                on_connected,
            )
        )

    def discover(self) -> list[dict]:
        """Open the calls page and return the visible interactive controls.

        Diagnostic helper for locating the dial UI; not used in normal flow.
        """
        if self._browser is None:
            raise RuntimeError('Caller not started; use it as a context manager.')
        return self._loop.run_until_complete(self._discover())

    async def _discover(self) -> list[dict]:
        import json

        await self._browser.get(f'{ORIGIN}/u/0/calls')
        await self._sleep(5)
        await self._install_helpers()
        raw = await self._action('window.__gv.dump()')
        return json.loads(raw) if raw else []

    async def _place_call(
        self,
        recipient: str,
        hold: float | None,
        wait_for_answer: bool,
        ring_timeout: float,
        on_connected=None,
    ) -> str:
        # _start already navigated to /calls; only reload if we drifted away.
        url = await self._tab.evaluate('location.href', await_promise=False)
        if '/calls' not in (url or ''):
            await self._browser.get(f'{ORIGIN}/u/0/calls')
            await self._sleep(5)
        else:
            await self._sleep(1)
        await self._install_helpers()

        # 1. focus the number field and type with REAL keystrokes -- the
        #    Material autocomplete only renders the "Call <number>" button in
        #    response to genuine key events, not a synthetic value set. The
        #    /calls page shows the dial panel directly; only older layouts hide
        #    it behind a "Make a call" button (handled as a fallback).
        if not await self._focus_number_field():
            await self._open_dialer()
            if not await self._focus_number_field():
                dump = await self._action('window.__gv.dump()')
                raise APIError(
                    f'Could not find the phone-number field. Visible controls: {dump}'
                )
        await self._type(recipient)
        await self._sleep(1.5)  # let the "Call <number>" button render

        # 2. start the call: click the "Call <number>" button
        if not await self._click_call_button(recipient):
            dump = await self._action('window.__gv.dump()')
            raise APIError(
                f'No "Call {recipient}" action appeared. Visible controls: {dump}'
            )

        # 3. wait through ringing until the line connects (or give up)
        if wait_for_answer:
            state = await self._await_answer(ring_timeout)
            if state != 'connected':
                outcome = 'no-answer' if state == 'no-answer' else 'declined'
                log.info('not connected (%s); hanging up', state)
                await self._hangup()
                return outcome

        # 4. connected. Hand off to on_connected (e.g. the realtime bridge) if
        #    given; otherwise just hold so the fake-mic audio plays.
        if on_connected is not None:
            log.info('connected to %s; handing off to on_connected', recipient)
            try:
                await on_connected(self)
            finally:
                await self._hangup()
            return 'completed'
        if hold is None:
            hold = 20.0
        log.info('connected to %s; holding up to %.1fs', recipient, hold)
        remote_ended = await self._hold_while_connected(hold)
        await self._hangup()
        return 'remote-ended' if remote_ended else 'completed'

    # ------------------------------------------------------------------ #
    # UI steps (each tries a few label variants the GV web app may use)
    # ------------------------------------------------------------------ #
    async def _open_dialer(self) -> bool:
        for label in ('Make a call', 'New call', 'Calls', 'Call'):
            if await self._poll(f"window.__gv.clickAria({label!r})", tries=4) == 'OK':
                return True
        for sub in ('make a call', 'new call', 'dial'):
            if await self._poll(f"window.__gv.clickAriaLike({sub!r})", tries=2) == 'OK':
                return True
        return False

    async def _focus_number_field(self) -> bool:
        for ph in (
            'Enter a name or phone number',
            'Type a name or phone number',
            'Enter a name or number',
            'phone number',
        ):
            if await self._poll(f"window.__gv.focus({ph!r})", tries=4) == 'OK':
                return True
        return False

    async def _click_call_button(self, recipient: str) -> bool:
        # The dialer's call button has aria-label "Call <spaced E.164>", e.g.
        # "Call + 1 2 0 8 9 9 9 7 7 0 9". Match it EXACTLY: the call-history rows
        # share the ``call-button`` class, so a class lookup would click a
        # call-back button for some past contact instead of dialing our number.
        spaced = ' '.join(recipient)
        label = f'Call {spaced}'
        if await self._poll(f"window.__gv.clickAria({label!r})", tries=6) == 'OK':
            return True
        # fallback: any action whose label contains the spaced number
        if await self._poll(f"window.__gv.clickAriaLike({spaced!r})", tries=4) == 'OK':
            return True
        return False

    # ------------------------------------------------------------------ #
    # call-state machine (read from the "Call panel" DOM)
    # ------------------------------------------------------------------ #
    async def _call_state(self) -> str:
        """Classify the live call: 'idle' | 'ringing' | 'connected' | 'ended'."""
        import json

        raw = await self._action('window.__gv.callPanel()')
        try:
            p = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            p = {}
        cls, txt = p.get('cls', ''), p.get('txt', '')
        if not p.get('present') or 'no-active-call' in cls:
            return 'idle'
        if 'Call ended' in txt:
            return 'ended'
        if 'Calling' in txt:
            return 'ringing'
        if 'Elapsed time' in txt or re.search(r'\d?\d:\d\d', txt):
            return 'connected'
        return 'ringing'  # active panel, not yet connected -> treat as ringing

    async def _await_answer(self, ring_timeout: float) -> str:
        """Wait through ringing. Returns 'connected', 'no-answer', or 'ended'."""
        seen_active = False
        for _ in range(int(max(1.0, ring_timeout) / 0.5)):
            state = await self._call_state()
            if state in ('ringing', 'connected'):
                seen_active = True
            if state == 'connected':
                return 'connected'
            # only honor a terminal state once the call has actually started
            if seen_active and state in ('ended', 'idle'):
                return 'ended'
            await self._sleep(0.5)
        return 'no-answer'

    async def _hold_while_connected(self, hold: float) -> bool:
        """Stay on the line for ``hold`` s; return True if it ended remotely."""
        for _ in range(int(max(0.0, hold) / 0.5)):
            await self._sleep(0.5)
            if await self._call_state() in ('ended', 'idle'):
                log.info('the other party ended the call')
                return True
        return False

    async def _hangup(self) -> None:
        # Best-effort: clicking "End call" is tidy, but close() (browser.stop)
        # ends the call anyway -- so never let a dropped CDP connection or a
        # vanished call panel turn teardown into a crash.
        try:
            for sub in ('end call', 'hang up'):
                if await self._poll(f"window.__gv.clickAriaLike({sub!r})", tries=3) == 'OK':
                    break
            log.info('hung up')
        except Exception as exc:  # noqa: BLE001
            log.debug('hangup best-effort failed (%r); closing browser instead', exc)

    # ------------------------------------------------------------------ #
    # primitives (mirrors googlevoice.browser)
    # ------------------------------------------------------------------ #
    async def _install_helpers(self) -> None:
        await self._tab.evaluate(_HELPERS_JS, await_promise=False)
        await self._tab.evaluate(_CALL_HELPERS_JS, await_promise=False)

    async def _action(self, expr: str):
        """Evaluate ``expr``; reinstall the JS helpers and retry once if needed."""
        try:
            return await self._tab.evaluate(expr, await_promise=False)
        except Exception:
            await self._install_helpers()
            return await self._tab.evaluate(expr, await_promise=False)

    async def _poll(self, expr: str, *, tries: int = 20, delay: float = 0.5) -> str:
        res = 'MISS'
        for _ in range(tries):
            res = await self._action(expr)
            if res == 'OK':
                return res
            await self._sleep(delay)
        return res

    async def _type(self, text: str) -> None:
        """Type ``text`` into the focused element with real (trusted) keystrokes."""
        from nodriver import cdp

        for ch in text:
            await self._tab.send(
                cdp.input_.dispatch_key_event(type_='keyDown', text=ch, key=ch)
            )
            await self._tab.send(cdp.input_.dispatch_key_event(type_='keyUp', key=ch))
            await self._sleep(0.04)

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

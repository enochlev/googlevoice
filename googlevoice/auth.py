"""
Authentication for the modern Google Voice web API.

Google retired the legacy ``/voice/b/0/`` HTML/XML endpoints this library was
originally built on.  The current voice.google.com web app talks to

    https://clients6.google.com/voice/v1/voiceclient/...

and authenticates each request with a ``SAPISIDHASH`` -- a hash computed from
the Google login cookies, the request origin, and a timestamp.  It needs no
OAuth dance and never expires (the cookies do the work), which makes it ideal
for a *portable* credential.

This module splits authentication cleanly in two:

* **Authenticate once, with a browser** -- :func:`browser_login` drives a real
  (undetected) Chrome via ``nodriver`` so you can sign in to Google normally
  (password, 2FA, passkeys and captcha all just work).  It then harvests the
  Google cookies and writes them to a portable ``session.json``.

* **Operate anywhere, without a browser** -- :class:`Credentials` turns those
  saved cookies into the ``Authorization`` header every API call needs, using
  nothing but :mod:`requests` and :mod:`hashlib`.  Copy ``session.json`` to a
  headless server and it keeps working.

The only machine-specific requirement is that Chrome is installed, and only for
the one-time :func:`browser_login`.  Everything after that is pure ``requests``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import pathlib
import sys
import time

import requests

log = logging.getLogger(__name__)

# The voice.google.com web client's public API key and origin, observed from
# its network traffic.  The key is a browser key (not a secret); it merely
# identifies the API project.
ORIGIN = 'https://voice.google.com'
API_KEY = 'AIzaSyDTYc1N4xiODyrQYK0Kl6g_y279LjYkrBg'
API_BASE = 'https://clients6.google.com/voice/v1/voiceclient/'

# A plausible desktop-Chrome UA.  The API authenticates on cookies + hash, not
# on this, but a real-looking UA avoids standing out.
USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
)

# (header-label, cookie-name) pairs whose SHA1 forms the multi-part
# ``Authorization`` value the web app sends.  Any present subset is accepted by
# the server; we send all we have to match the real client exactly.
_HASH_COOKIES = (
    ('SAPISIDHASH', 'SAPISID'),
    ('SAPISID1PHASH', '__Secure-1PAPISID'),
    ('SAPISID3PHASH', '__Secure-3PAPISID'),
)

# Where the portable credentials live by default.  A directory (not the legacy
# ``~/.gvoice`` config *file*) so the one-time browser profile can sit beside it.
DEFAULT_DIR = pathlib.Path.home() / '.googlevoice'
DEFAULT_SESSION_PATH = DEFAULT_DIR / 'session.json'
DEFAULT_PROFILE_DIR = DEFAULT_DIR / 'chrome-profile'

# Cookies that signal a usable Google login (used to detect login completion).
ESSENTIAL_COOKIES = {'SID', 'SAPISID', '__Secure-3PSID'}


# --------------------------------------------------------------------------- #
# Browser launch helpers (shared by browser-driven features)
# --------------------------------------------------------------------------- #
def headless_default() -> bool:
    """
    Whether to run Chrome headless by default.

    On Linux a *headed* Chrome cannot start without a display server, so if
    neither ``DISPLAY`` nor ``WAYLAND_DISPLAY`` is set (a headless server,
    container, CI, or a tool that spawns us without a display) we must go
    headless. On macOS/Windows headed always works, so default to headed.
    """
    if sys.platform.startswith('linux'):
        return not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
    return False


def browser_launch_args(*, no_sandbox: bool | None = None) -> list[str]:
    """
    Common ``browser_args`` for ``nodriver``/Chrome.

    ``--no-sandbox`` is added only when running as root (``no_sandbox=None``),
    since Chrome's sandbox can't initialize as root/in many containers and
    refuses to launch; on a normal user account it stays on (safer).
    """
    args = ['--no-first-run', '--no-default-browser-check']
    if no_sandbox is None:
        no_sandbox = os.name == 'posix' and getattr(os, 'geteuid', lambda: 1)() == 0
    if no_sandbox:
        args.append('--no-sandbox')
    return args


class AuthError(Exception):
    """Authentication failed, or the saved session is missing/expired."""


def _is_google_cookie(domain: str) -> bool:
    domain = domain.lstrip('.')
    return domain == 'google.com' or domain.endswith('.google.com')


# --------------------------------------------------------------------------- #
# Session (cookie) persistence
# --------------------------------------------------------------------------- #
def save_session(
    cookies: list[dict], path: pathlib.Path = DEFAULT_SESSION_PATH
) -> pathlib.Path:
    """Write harvested cookies to ``path`` as portable JSON (mode 0600)."""
    path = pathlib.Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({'version': 1, 'cookies': cookies}, indent=2), encoding='utf-8'
    )
    with contextlib.suppress(OSError):
        path.chmod(0o600)  # credentials -- keep them to ourselves
    return path


def load_session(path: pathlib.Path = DEFAULT_SESSION_PATH) -> list[dict]:
    """Load cookies previously saved by :func:`save_session`."""
    path = pathlib.Path(path).expanduser()
    if not path.exists():
        raise AuthError(
            f'No saved session at {path}. Run `python -m googlevoice.auth login` first.'
        )
    return json.loads(path.read_text(encoding='utf-8'))['cookies']


# --------------------------------------------------------------------------- #
# Credentials: cookies -> Authorization header
# --------------------------------------------------------------------------- #
def sapisid_hash(cookies: list[dict], *, now: float | None = None) -> str:
    """
    Build the multi-part ``SAPISIDHASH`` ``Authorization`` value.

    Each part is ``<label> <ts>_<sha1(f"{ts} {cookie} {ORIGIN}")>``.
    """
    ts = int(time.time() if now is None else now)
    by_name = {c['name']: c['value'] for c in cookies}
    parts = []
    for label, name in _HASH_COOKIES:
        secret = by_name.get(name)
        if secret:
            digest = hashlib.sha1(f'{ts} {secret} {ORIGIN}'.encode()).hexdigest()
            parts.append(f'{label} {ts}_{digest}')
    if not parts:
        raise AuthError(
            'No SAPISID cookies in the session -- it is invalid or expired. '
            'Re-run `python -m googlevoice.auth login`.'
        )
    return ' '.join(parts)


class Credentials:
    """
    Portable Google Voice credentials: a cookie jar plus the logic to sign
    requests to the modern API.  Construct from a saved session and hand to
    :class:`googlevoice.voice.Voice`.
    """

    def __init__(self, cookies: list[dict]):
        self.cookies = cookies

    @classmethod
    def load(cls, path: pathlib.Path = DEFAULT_SESSION_PATH) -> Credentials:
        """Load credentials from a saved ``session.json``."""
        return cls(load_session(path))

    def requests_session(self) -> requests.Session:
        """A :class:`requests.Session` preloaded with the Google cookies."""
        sess = requests.Session()
        for c in self.cookies:
            # Preserve the original (often dotted, e.g. ``.google.com``) domain
            # so the cookies are sent to the ``clients6.google.com`` subdomain.
            sess.cookies.set(
                c['name'], c['value'], domain=c['domain'], path=c.get('path', '/')
            )
        return sess

    def auth_headers(self) -> dict[str, str]:
        """Fresh request headers, including a freshly-timestamped SAPISIDHASH."""
        return {
            'Authorization': sapisid_hash(self.cookies),
            'Content-Type': 'application/json+protobuf',
            'X-Goog-Api-Key': API_KEY,
            'X-Goog-AuthUser': '0',
            # The logical origin travels in X-Origin (NOT Origin): the gapi
            # client proxies through an iframe on the API host, so a literal
            # ``Origin: voice.google.com`` against host ``clients6.google.com``
            # trips Google's cross-domain check ("Origin doesn't match Host").
            'X-Origin': ORIGIN,
            'X-Referer': ORIGIN,
            'X-Requested-With': 'XMLHttpRequest',
            'User-Agent': USER_AGENT,
        }


# --------------------------------------------------------------------------- #
# Login probe (pure requests) -- "are these cookies usable right now?"
# --------------------------------------------------------------------------- #
def session_is_valid(cookies: list[dict]) -> bool:
    """True if the cookies can authenticate a live ``account/get`` call."""
    try:
        creds = Credentials(cookies)
        resp = creds.requests_session().post(
            API_BASE + 'account/get',
            params={'alt': 'json', 'key': API_KEY},
            headers=creds.auth_headers(),
            data='[null,1]',
            timeout=30,
        )
        return resp.status_code == 200
    except (requests.RequestException, AuthError) as err:
        log.debug('session probe failed: %s', err)
        return False


# --------------------------------------------------------------------------- #
# Browser login (one-time, needs Chrome)
# --------------------------------------------------------------------------- #
async def _browser_login_async(
    profile_dir: pathlib.Path, *, headless: bool, timeout: float, poll: float
):
    import asyncio

    import nodriver as uc

    profile_dir = pathlib.Path(profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)

    browser = await uc.start(
        headless=headless,
        user_data_dir=str(profile_dir),
        browser_args=browser_launch_args(),
    )
    try:
        await browser.get(ORIGIN)
        print('>>> A Chrome window opened. Sign in to the Google account that')
        print('>>> owns your Google Voice number, then wait on the Voice inbox.')
        print('>>> Detecting login automatically...')

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cookies = [
                {
                    'name': c.name,
                    'value': c.value,
                    'domain': c.domain,
                    'path': c.path or '/',
                    'secure': bool(getattr(c, 'secure', False)),
                    'expires': getattr(c, 'expires', None),
                }
                for c in await browser.cookies.get_all()
                if _is_google_cookie(c.domain)
            ]
            names = {c['name'] for c in cookies}
            # Confirm by actually authenticating, not just by cookie presence.
            if ESSENTIAL_COOKIES <= names and session_is_valid(cookies):
                print('>>> Login confirmed (account/get succeeded).')
                return cookies
            await asyncio.sleep(poll)
        raise AuthError(f'Timed out after {timeout:.0f}s waiting for login.')
    finally:
        browser.stop()


def browser_login(
    session_path: pathlib.Path = DEFAULT_SESSION_PATH,
    profile_dir: pathlib.Path = DEFAULT_PROFILE_DIR,
    *,
    headless: bool = False,
    timeout: float = 300,
    poll: float = 3,
) -> Credentials:
    """
    Open a browser, let the user sign in to Google, then persist the session.

    Returns ready-to-use :class:`Credentials`; also writes ``session_path``.
    """
    import nodriver as uc

    cookies = uc.loop().run_until_complete(
        _browser_login_async(profile_dir, headless=headless, timeout=timeout, poll=poll)
    )
    saved = save_session(cookies, session_path)
    print(f'>>> Saved {len(cookies)} cookies to {saved}')
    return Credentials(cookies)


# --------------------------------------------------------------------------- #
# CLI: python -m googlevoice.auth login | check
# --------------------------------------------------------------------------- #
def _main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog='python -m googlevoice.auth')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_login = sub.add_parser('login', help='Sign in via browser and save a session')
    p_login.add_argument('--session', default=str(DEFAULT_SESSION_PATH))
    p_login.add_argument('--profile', default=str(DEFAULT_PROFILE_DIR))
    p_login.add_argument('--headless', action='store_true')
    p_login.add_argument('--timeout', type=float, default=300)

    p_check = sub.add_parser('check', help='Verify a saved session still works')
    p_check.add_argument('--session', default=str(DEFAULT_SESSION_PATH))

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format='%(levelname)s %(name)s: %(message)s'
    )

    if args.cmd == 'login':
        browser_login(
            session_path=pathlib.Path(args.session),
            profile_dir=pathlib.Path(args.profile),
            headless=args.headless,
            timeout=args.timeout,
        )
        print('>>> Done. The session is ready to use from any machine.')
    elif args.cmd == 'check':
        ok = session_is_valid(load_session(pathlib.Path(args.session)))
        print('Session is VALID.' if ok else 'Session is INVALID or expired.')
        raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    _main()

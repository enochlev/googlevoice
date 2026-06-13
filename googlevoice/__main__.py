"""
Command-line interface for googlevoice.  Invoke with ``python -m googlevoice``.

    python -m googlevoice login                # one-time browser sign-in
    python -m googlevoice check                # is the saved session still valid?
    python -m googlevoice number               # print your Google Voice number
    python -m googlevoice inbox [-n N]         # list recent conversations
    python -m googlevoice thread NUMBER [-n N] # show up to N messages with NUMBER
    python -m googlevoice send NUMBER TEXT     # send an SMS (drives a browser)

Add ``--json`` to ``number``/``inbox``/``thread`` for machine-readable output.
Numbers may be given formatted or bare (e.g. ``2085551234``); a missing country
code defaults to +1. If the saved session is missing or expired, a command
offers to sign in (via the browser) and then retries automatically.
"""

import argparse
import json

from . import auth, util
from .voice import Voice


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m googlevoice', description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('login', help='Sign in via browser and save a portable session')
    sub.add_parser('check', help='Check whether the saved session still works')

    p_number = sub.add_parser('number', help='Print your Google Voice number')
    p_number.add_argument('--json', action='store_true', help='machine-readable output')

    p_inbox = sub.add_parser('inbox', help='List recent conversations')
    p_inbox.add_argument(
        '-n',
        '--count',
        type=int,
        default=20,
        help='how many conversations (default 20)',
    )
    p_inbox.add_argument('--json', action='store_true', help='machine-readable output')

    p_thread = sub.add_parser('thread', help='Show the conversation with a number')
    p_thread.add_argument('number', help='The other party, e.g. +12085551234')
    p_thread.add_argument(
        '-n',
        '--count',
        type=int,
        default=50,
        help='show up to N recent messages (default 50)',
    )
    p_thread.add_argument('--json', action='store_true', help='machine-readable output')

    p_send = sub.add_parser('send', help='Send an SMS (launches a browser)')
    p_send.add_argument('number', help='Recipient, e.g. +12085551234')
    p_send.add_argument('text', nargs='+', help='Message text')

    return parser


def _confirm(question: str) -> bool:
    try:
        return input(f'{question} [Y/n] ').strip().lower() in ('', 'y', 'yes')
    except EOFError:
        return False


def _cmd_number(args) -> None:
    number = Voice().number
    print(json.dumps({'number': number}) if args.json else number)


def _cmd_inbox(args) -> None:
    threads = Voice().inbox(count=args.count)
    if args.json:
        print(json.dumps([t.as_dict() for t in threads], indent=2))
        return
    for thread in threads:
        flag = ' ' if thread.read else '*'
        print(f'{flag} {thread.contact}: {thread.latest_text!r}')


def _cmd_thread(args) -> None:
    thread = Voice().thread(args.number, messages=args.count)
    if thread is None:
        print(
            'null'
            if args.json
            else f'No conversation with {args.number} in recent threads.'
        )
        raise SystemExit(1)
    if args.json:
        print(json.dumps(thread.as_dict(), indent=2))
        return
    for msg in reversed(thread.messages):  # oldest first
        arrow = '<-' if msg.incoming else '->'
        # local time, matching the Google Voice web UI
        when = (
            msg.start_time.astimezone().strftime('%Y-%m-%d %H:%M')
            if msg.start_time
            else '?'
        )
        print(f'[{when}] {arrow} {msg.text!r}')


def _cmd_send(args) -> None:
    from .browser import BrowserSender

    with BrowserSender() as sender:
        sender.send_sms(args.number, ' '.join(args.text))
    print('Sent.')


_COMMANDS = {
    'number': _cmd_number,
    'inbox': _cmd_inbox,
    'thread': _cmd_thread,
    'send': _cmd_send,
}


def _run(args) -> None:
    """Run a session-backed command (may raise on a missing/expired session)."""
    _COMMANDS[args.cmd](args)


def main(argv=None) -> None:
    args = _build_parser().parse_args(argv)

    if args.cmd == 'login':
        auth.browser_login()
        print('Done. The session is ready to use from any machine.')
        return

    if args.cmd == 'check':
        try:
            ok = auth.session_is_valid(auth.load_session())
        except auth.AuthError:
            ok = False
        print('Session is VALID.' if ok else 'Session is INVALID or missing.')
        raise SystemExit(0 if ok else 1)

    try:
        _run(args)
    except (auth.AuthError, util.LoginError) as err:
        print(f'\n{err}\n')
        if _confirm('Sign in with a browser now and retry?'):
            auth.browser_login()
            _run(args)
        else:
            raise SystemExit(1) from None


if __name__ == '__main__':
    main()

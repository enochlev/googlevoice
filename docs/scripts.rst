.. _scripts:


Command Line
============

The package is runnable as a module::

    python -m googlevoice <command>

Commands
--------

::

    login              One-time browser sign-in; saves a portable session.
    check              Report whether the saved session is still valid.
    number             Print your Google Voice number.
    inbox [-n N]       List the N most recent conversations (default 20).
    thread NUMBER [-n N]  Show up to N recent messages with NUMBER (default 50).
    send NUMBER TEXT   Send an SMS to NUMBER, launching a browser.

``number``, ``inbox`` and ``thread`` accept ``--json`` for machine-readable
output. Numbers may be formatted or bare -- ``+1 (208) 555-1234``,
``208-555-1234`` and ``2085551234`` are all accepted (a missing country code
defaults to +1). If the saved session has expired, a command offers to sign in
and then retries.

Examples
--------

::

    $ python -m googlevoice login
    $ python -m googlevoice number
    +12085551234
    $ python -m googlevoice inbox -n 5
    $ python -m googlevoice send +12085550000 "Hello from the command line"
    Sent.

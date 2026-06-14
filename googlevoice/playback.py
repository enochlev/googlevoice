"""
Speak a TTS message into a live Google Voice call -- no OpenAI involved.

This is the simple, standalone counterpart to :mod:`googlevoice.realtime`:
it speaks a fixed message rather than holding a conversation.

Crucially, audio is streamed into Chrome's microphone (via a PipeWire null sink)
only AFTER the callee answers, so it always starts at the first word -- nothing
is ever heard before pickup. Shares the PipeWire plumbing in
:mod:`googlevoice._pwaudio` but not the realtime/OpenAI code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from . import audio
from ._pwaudio import MIC_CONSTRAINTS_JS, create_null_sink, destroy_sink, relink_mic
from .call import Caller

log = logging.getLogger(__name__)

MIC_SINK = 'gv_mic'


class _Player:
    """on_connected hook: relink the mic, then play ``wav`` once after pickup."""

    def __init__(self, wav: str, *, mic_sink: str = MIC_SINK):
        self.wav = wav
        self.mic_sink = mic_sink
        self._proc = None

    async def __call__(self, caller: Caller) -> None:
        await relink_mic(self.mic_sink)
        player = asyncio.create_task(self._play_once())
        watcher = asyncio.create_task(self._until_call_ends(caller))
        _, pending = await asyncio.wait(
            {player, watcher}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
        await asyncio.sleep(0.3)  # let the last frames flush before hangup

    async def _play_once(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            'pw-play', '--target', self.mic_sink, self.wav,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._proc.wait()

    async def _until_call_ends(self, caller: Caller) -> None:
        while True:
            await asyncio.sleep(0.5)
            if await caller._call_state() in ('ended', 'idle'):
                log.info('caller hung up during playback')
                return


def place_say_call(
    number: str,
    say: str,
    *,
    voice: str = audio.DEFAULT_VOICE,
    lead_silence: float = audio.DEFAULT_LEAD_SILENCE,
    ring_timeout: float = 45.0,
) -> str:
    """Call ``number`` and, once answered, speak ``say`` via TTS.

    Returns the call outcome (see :meth:`googlevoice.call.Caller.place_call`).
    """
    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    tmp.close()
    try:
        audio.tts_to_wav(say, tmp.name, voice=voice, lead_silence=lead_silence)

        create_null_sink(MIC_SINK)
        try:
            with Caller(
                extra_browser_args=['--use-fake-ui-for-media-stream'],
                init_script=MIC_CONSTRAINTS_JS,
            ) as caller:
                return caller.place_call(
                    number, wait_for_answer=True, ring_timeout=ring_timeout,
                    on_connected=_Player(tmp.name),
                )
        finally:
            destroy_sink(MIC_SINK)
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)

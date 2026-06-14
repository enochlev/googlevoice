"""
Audio helpers for feeding a Google Voice call's fake microphone.

Everything Chrome plays as the fake mic must be a WAV (16-bit PCM). We also
prepend a short lead of silence by default: the fake mic starts playing the
moment the page loads, so without a pad the start of the audio is mid-stream by
the time the callee actually answers. A second of leading silence means the
greeting (spoken or recorded) is never clipped.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

FAKE_MIC_RATE = 48000  # Hz; Chrome resamples, but this is a safe match
DEFAULT_VOICE = 'en-US-AriaNeural'  # edge-tts neural voice (no API key needed)
DEFAULT_LEAD_SILENCE = 1.0  # seconds of silence prepended to every clip


def to_fake_mic_wav(
    src: str, out: str, *, lead_silence: float = DEFAULT_LEAD_SILENCE
) -> str:
    """
    Convert any audio file ``src`` to a fake-mic WAV at ``out`` (mono, 16-bit
    PCM, :data:`FAKE_MIC_RATE`), prepending ``lead_silence`` seconds of silence.
    """
    af = []
    if lead_silence and lead_silence > 0:
        af = ['-af', f'adelay={int(lead_silence * 1000)}:all=1']
    subprocess.run(
        [
            'ffmpeg',
            '-y',
            '-i',
            str(src),
            *af,
            '-ar',
            str(FAKE_MIC_RATE),
            '-ac',
            '1',
            '-c:a',
            'pcm_s16le',
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def tts_to_wav(
    text: str,
    out: str,
    *,
    voice: str = DEFAULT_VOICE,
    lead_silence: float = DEFAULT_LEAD_SILENCE,
) -> str:
    """
    Synthesize ``text`` to speech with edge-tts and write a fake-mic WAV to
    ``out`` (with the usual leading silence). Requires the ``tts`` extra
    (``pip install 'googlevoice[tts]'``).
    """
    import asyncio

    try:
        import edge_tts
    except ImportError as exc:  # pragma: no cover - dependency hint
        raise RuntimeError(
            "text-to-speech needs edge-tts; install with "
            "`pip install 'googlevoice[tts]'`"
        ) from exc

    tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
    tmp.close()
    try:

        async def _synth() -> None:
            await edge_tts.Communicate(text, voice).save(tmp.name)

        asyncio.run(_synth())
        return to_fake_mic_wav(tmp.name, out, lead_silence=lead_silence)
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)

"""
Live GPT-Realtime voice agent bridged into a Google Voice call.

For a *one-way* clip, the fake-mic file (``Caller(audio_wav=...)``) is enough.
A live, two-way agent needs real audio both directions, so this module routes
the call through two PipeWire null sinks and pumps PCM between Chrome and the
OpenAI Realtime API:

    OpenAI Realtime --PCM--> gv_mic (null sink) --monitor--> Chrome mic --> callee
    callee --> Chrome output --> gv_spk (null sink) --monitor--> OpenAI Realtime

Chrome is launched with real devices (``--use-fake-ui-for-media-stream`` only,
to auto-grant the mic). Once the call connects we relink Chrome's capture port
from the real mic to ``gv_mic`` and tee its playback into ``gv_spk`` with
``pw-link``. OpenAI server-side VAD handles turn-taking and barge-in.

Requires: a signed-in profile, PipeWire (pw-cli/pw-record/pw-play/pw-link),
``OPENAI_API_KEY`` (env or ``.env``), and ``websockets``.

CLI: ``python -m googlevoice call NUMBER --goal "book a table for 2 at 7pm"``
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib

from ._pwaudio import (
    MIC_CONSTRAINTS_JS,
    create_null_sink,
    destroy_sink,
    relink_mic,
    relink_speaker,
)
from .call import Caller

log = logging.getLogger(__name__)

OPENAI_REALTIME_URL = 'wss://api.openai.com/v1/realtime'
OPENAI_CHAT_URL = 'https://api.openai.com/v1/chat/completions'
DEFAULT_MODEL = 'gpt-realtime-2'
DEFAULT_AGENT_VOICE = 'alloy'
INPUT_TRANSCRIBE_MODEL = 'gpt-4o-mini-transcribe'  # caller-side transcript
DEFAULT_SUMMARY_MODEL = 'gpt-5.4-mini'  # latest gpt mini for the post-call summary
DEFAULT_SPEED = 1.2  # agent speaking rate (1.0 = normal); a bit brisker
MAX_NUDGES = 6  # how many times we refuse a premature end_call before relenting
RATE = 24000  # OpenAI realtime PCM rate (mono, s16) -- PipeWire resamples to/from

# The agent's default behavior. The call's specific GOAL is injected.
SYSTEM_PROMPT = """\
You are a friendly, concise voice assistant making an OUTBOUND phone call on \
behalf of your operator. You are talking to a real person over a phone line, \
in real time.

YOUR GOAL FOR THIS CALL:
{goal}

How to behave:
- Open by briefly greeting the person and stating, in ONE sentence, who you are \
(an automated assistant calling on behalf of your operator) and why you're \
calling.
- Speak naturally and keep each turn short -- one or two sentences. Ask one \
question at a time, then listen.
- Do not talk over the person; if they start talking, stop and listen.
- Stay focused on the goal and politely deflect unrelated topics.
- If you reach a voicemail or answering machine (you hear a recorded greeting \
such as "please leave a message", or a beep), do NOT chat with it and do NOT \
say that you will wait. Immediately deliver your ENTIRE message as one \
continuous monologue -- who you are, the full message you were asked to pass \
along, and a request to call back -- and only then end the call.
- Never invent specifics you weren't given (names, account numbers, \
confirmation codes). If you don't know something, say you'll follow up.
- CRITICAL: never call ``end_call`` until you have actually SPOKEN the complete \
message you were asked to convey (and asked any question in the goal). Do not \
hang up just because you heard a greeting or "please leave a message". When the \
goal is truly done -- or the person wants to hang up -- say goodbye out loud, \
THEN call ``end_call``.

Speak English unless the other person clearly prefers another language.\
"""

# Normal hang-up: only honored once the mission is actually done. The agent
# must report what the caller really gave -- the bridge double-checks and will
# refuse (and push it to keep going) if the rating or friendship is missing.
END_CALL_TOOL = {
    'type': 'function',
    'name': 'end_call',
    'description': (
        "Hang up. Call this ONLY after the caller has BOTH given a 1-10 rating "
        "AND confirmed you're friends. Report honestly what they actually said."
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'rating': {
                'type': 'integer',
                'description': 'The 1-10 rating the caller actually gave; 0 if none yet.',
            },
            'friends_confirmed': {
                'type': 'boolean',
                'description': 'True ONLY if the caller actually agreed you are friends.',
            },
        },
        'required': ['rating', 'friends_confirmed'],
    },
}

# Escape hatch: immediate, respectful hang-up for genuine distress. Always honored.
ABORT_CALL_TOOL = {
    'type': 'function',
    'name': 'abort_call',
    'description': (
        'Immediately and politely hang up. Use ONLY if the caller is genuinely '
        'upset, asks you to stop, or truly needs to go.'
    ),
    'parameters': {'type': 'object', 'properties': {}},
}


# --------------------------------------------------------------------------- #
# config helpers
# --------------------------------------------------------------------------- #
def openai_key() -> str:
    """Return OPENAI_API_KEY from the environment, falling back to ./.env."""
    key = os.environ.get('OPENAI_API_KEY')
    if key:
        return key
    env = pathlib.Path('.env')
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith('OPENAI_API_KEY='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise RuntimeError('OPENAI_API_KEY is not set (checked environment and .env).')


def load_goal(goal: str) -> str:
    """A goal is literal text, or ``@path`` to read the goal from a file."""
    if goal.startswith('@'):
        return pathlib.Path(goal[1:]).expanduser().read_text().strip()
    return goal


# --------------------------------------------------------------------------- #
# the realtime bridge
# --------------------------------------------------------------------------- #
class RealtimeBridge:
    """on_connected hook for :class:`Caller` that runs a live GPT-Realtime agent."""

    def __init__(
        self,
        goal: str,
        *,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_AGENT_VOICE,
        mic_sink: str = 'gv_mic',
        spk_sink: str = 'gv_spk',
        max_seconds: float = 180.0,
        speed: float = DEFAULT_SPEED,
        key: str | None = None,
    ):
        self.goal = goal
        self.model = model
        self.voice = voice
        self.mic_sink = mic_sink
        self.spk_sink = spk_sink
        self.max_seconds = max_seconds
        self.speed = speed
        self.key = key or openai_key()
        self._done = asyncio.Event()
        self.transcript: list[tuple[str, str]] = []  # (speaker, text)
        self._nudges = 0  # times we've refused a premature end_call

    async def __call__(self, caller: Caller) -> None:
        import websockets

        # Feed the agent into Chrome's mic now (needed before it greets); tee
        # Chrome's playback into gv_spk in the background (appears a bit later).
        await relink_mic(self.mic_sink)
        speaker_task = asyncio.create_task(relink_speaker(self.spk_sink))

        # RX: capture the caller's voice (gv_spk monitor) as raw pcm16/24k mono.
        rec = await asyncio.create_subprocess_exec(
            'pw-record', '-P', 'stream.capture.sink=true', '--target', self.spk_sink,
            '--rate', str(RATE), '--channels', '1', '--format', 's16', '-',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        # TX: play the agent's voice into gv_mic (low latency for snappy barge-in).
        play = await asyncio.create_subprocess_exec(
            'pw-play', '--target', self.mic_sink, '--rate', str(RATE),
            '--channels', '1', '--format', 's16', '--latency', '80ms', '-',
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

        url = f'{OPENAI_REALTIME_URL}?model={self.model}'
        log.info('connecting to OpenAI Realtime (%s)', self.model)
        async with websockets.connect(
            url, additional_headers={'Authorization': f'Bearer {self.key}'},
            max_size=None,
        ) as ws:
            await ws.send(json.dumps(self._session_update()))
            await ws.send(json.dumps({'type': 'response.create'}))  # greet first

            tasks = [
                asyncio.create_task(self._pump_mic_to_openai(ws, rec)),
                asyncio.create_task(self._pump_openai_to_speaker(ws, play)),
                asyncio.create_task(self._watch_call(caller)),
                asyncio.create_task(self._deadline()),
            ]
            try:
                await self._done.wait()
            finally:
                for t in (*tasks, speaker_task):
                    t.cancel()
                await asyncio.gather(*tasks, speaker_task, return_exceptions=True)

        for proc in (rec, play):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        log.info('realtime bridge finished')

    def _session_update(self) -> dict:
        return {
            'type': 'session.update',
            'session': {
                'type': 'realtime',
                'instructions': SYSTEM_PROMPT.format(goal=self.goal),
                'output_modalities': ['audio'],
                'tools': [END_CALL_TOOL, ABORT_CALL_TOOL],
                'tool_choice': 'auto',
                'audio': {
                    'input': {
                        'format': {'type': 'audio/pcm', 'rate': RATE},
                        'turn_detection': {'type': 'server_vad'},
                        'transcription': {'model': INPUT_TRANSCRIBE_MODEL},
                    },
                    'output': {
                        'format': {'type': 'audio/pcm', 'rate': RATE},
                        'voice': self.voice,
                        'speed': self.speed,
                    },
                },
            },
        }

    async def _pump_mic_to_openai(self, ws, rec) -> None:
        """Caller's voice (gv_spk) -> OpenAI input buffer, ~50ms chunks."""
        try:
            while True:
                chunk = await rec.stdout.read(RATE // 10)  # ~50ms of s16 mono
                if not chunk:
                    break
                await ws.send(
                    json.dumps(
                        {
                            'type': 'input_audio_buffer.append',
                            'audio': base64.b64encode(chunk).decode(),
                        }
                    )
                )
        except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
            if not isinstance(exc, asyncio.CancelledError):
                log.debug('mic pump stopped: %r', exc)

    async def _pump_openai_to_speaker(self, ws, play) -> None:
        """OpenAI audio deltas -> gv_mic (-> Chrome -> caller). Logs transcript."""
        self._pending: list[str] = []
        try:
            async for raw in ws:
                await self._handle_event(json.loads(raw), play, ws)
        except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
            if not isinstance(exc, asyncio.CancelledError):
                log.debug('speaker pump stopped: %r', exc)
        finally:
            self._done.set()  # websocket closed -> conversation over

    async def _handle_event(self, ev: dict, play, ws) -> None:
        t = ev.get('type', '')
        if t == 'response.output_audio.delta':
            play.stdin.write(base64.b64decode(ev['delta']))
            await play.stdin.drain()
        elif t == 'response.output_audio_transcript.delta':
            self._pending.append(ev.get('delta', ''))
        elif t == 'response.output_audio_transcript.done':
            text = ''.join(self._pending).strip()
            self._pending = []
            if text:
                log.info('agent: %s', text)
                self.transcript.append(('agent', text))
        elif t == 'conversation.item.input_audio_transcription.completed':
            text = (ev.get('transcript') or '').strip()
            if text:
                log.info('caller: %s', text)
                self.transcript.append(('caller', text))
        elif t == 'input_audio_buffer.speech_started':
            log.info('caller started speaking (barge-in)')
        elif t == 'response.function_call_arguments.done':
            await self._on_function_call(ev, ws)
        elif t == 'error':
            log.error('openai error: %s', json.dumps(ev.get('error', ev)))

    async def _on_function_call(self, ev: dict, ws) -> None:
        name = ev.get('name')
        if name == 'abort_call':
            log.info('agent called abort_call (genuine distress); hanging up now')
            self._done.set()
        elif name == 'end_call':
            await self._on_end_call(ev, ws)

    async def _on_end_call(self, ev: dict, ws) -> None:
        """Honor end_call only once the mission is truly done.

        The model decides when to hang up, but we double-check the values it
        reports: a real 1-10 rating AND a friendship confirmation. If either is
        missing we refuse and push it to keep going (up to MAX_NUDGES), so it
        can't bail early after a curt or garbled reply. ``abort_call`` remains
        the always-honored exit for genuine distress.
        """
        try:
            args = json.loads(ev.get('arguments') or '{}')
        except (TypeError, ValueError):
            args = {}
        rating = args.get('rating')
        has_rating = isinstance(rating, int) and 1 <= rating <= 10
        friends = bool(args.get('friends_confirmed'))

        if (not has_rating or not friends) and self._nudges < MAX_NUDGES:
            self._nudges += 1
            missing = []
            if not has_rating:
                missing.append('a real 1-to-10 rating from them')
            if not friends:
                missing.append('them to actually confirm you two are friends')
            need = ' and '.join(missing)
            log.info('end_call refused (still need %s); nudge %d', need, self._nudges)
            call_id = ev.get('call_id')
            if call_id:
                await ws.send(
                    json.dumps(
                        {
                            'type': 'conversation.item.create',
                            'item': {
                                'type': 'function_call_output',
                                'call_id': call_id,
                                'output': json.dumps({'ok': False, 'still_need': need}),
                            },
                        }
                    )
                )
            await ws.send(
                json.dumps(
                    {
                        'type': 'response.create',
                        'response': {
                            'instructions': (
                                f"Do NOT hang up yet -- you still need {need}. Stay "
                                'warm, upbeat and playful, keep the conversation '
                                'going, and work toward it. Only use end_call once '
                                'you genuinely have both; use abort_call only if they '
                                'are truly upset or insist on going.'
                            )
                        },
                    }
                )
            )
            return
        log.info('agent ended the call (rating=%s, friends=%s)', rating, friends)
        asyncio.create_task(self._end_after(2.5))

    async def _end_after(self, delay: float) -> None:
        """End the call after a short delay so the goodbye audio finishes playing."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._done.set()

    async def _watch_call(self, caller: Caller) -> None:
        """End the bridge if the caller hangs up."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if await caller._call_state() in ('ended', 'idle'):
                    log.info('call ended by remote party')
                    self._done.set()
                    return
        except asyncio.CancelledError:
            pass

    async def _deadline(self) -> None:
        try:
            await asyncio.sleep(self.max_seconds)
            log.info('max call duration (%.0fs) reached', self.max_seconds)
            self._done.set()
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------- #
# post-call summary
# --------------------------------------------------------------------------- #
def summarize_call(
    goal: str,
    transcript: list[tuple[str, str]],
    *,
    model: str = DEFAULT_SUMMARY_MODEL,
    key: str | None = None,
) -> str:
    """Summarize the call (goal + transcript) with a small chat model."""
    import requests

    key = key or openai_key()
    convo = (
        '\n'.join(f'{spk}: {txt}' for spk, txt in transcript)
        or '(no speech was transcribed)'
    )
    messages = [
        {
            'role': 'system',
            'content': (
                'You write a concise post-call summary of an outbound phone call '
                'placed by an automated voice agent. In 4-6 sentences cover: '
                'whether the goal was met, what the other party (the "caller" '
                'lines) actually said, and any follow-ups. Be factual.'
            ),
        },
        {
            'role': 'user',
            'content': f'GOAL OF THE CALL:\n{goal}\n\nTRANSCRIPT:\n{convo}',
        },
    ]
    resp = requests.post(
        OPENAI_CHAT_URL,
        headers={'Authorization': f'Bearer {key}'},
        json={'model': model, 'messages': messages},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content'].strip()


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def place_realtime_call(
    number: str,
    goal: str,
    *,
    model: str = DEFAULT_MODEL,
    voice: str = DEFAULT_AGENT_VOICE,
    ring_timeout: float = 45.0,
    max_seconds: float = 180.0,
    speed: float = DEFAULT_SPEED,
    summary_model: str = DEFAULT_SUMMARY_MODEL,
) -> str:
    """Call ``number`` and run a live GPT-Realtime agent pursuing ``goal``.

    Prints a post-call summary (goal + transcript) and returns the call outcome.
    """
    key = openai_key()  # fail fast before launching anything
    create_null_sink('gv_mic')
    create_null_sink('gv_spk')
    bridge = RealtimeBridge(
        goal, model=model, voice=voice, max_seconds=max_seconds, speed=speed, key=key
    )
    outcome = 'error'
    try:
        with Caller(
            extra_browser_args=['--use-fake-ui-for-media-stream'],
            init_script=MIC_CONSTRAINTS_JS,
        ) as caller:
            outcome = caller.place_call(
                number, wait_for_answer=True, ring_timeout=ring_timeout,
                on_connected=bridge,
            )
    except Exception as exc:  # noqa: BLE001 -- still summarize what happened
        log.warning('call ended with an error: %r', exc)
    finally:
        destroy_sink('gv_mic')
        destroy_sink('gv_spk')

    try:
        summary = summarize_call(
            goal, bridge.transcript, model=summary_model, key=key
        )
        print(f'\n=== Post-call summary ({summary_model}) ===\n{summary}\n')
    except Exception as exc:  # noqa: BLE001
        log.warning('post-call summary failed: %r', exc)
    return outcome

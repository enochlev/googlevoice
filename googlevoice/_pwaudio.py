"""
PipeWire audio routing for browser-driven Google Voice calls.

Shared low-level plumbing used by both the TTS / audio-file playback path
(:mod:`googlevoice.playback`) and the live realtime agent
(:mod:`googlevoice.realtime`). Pure PipeWire + Chrome -- no OpenAI, no TTS.

The idea: route the call's audio through PipeWire null sinks so we can inject
audio into Chrome's microphone and/or capture what Chrome plays. Chrome runs
with real devices (``--use-fake-ui-for-media-stream`` only); once a call is up
we relink its capture/playback ports with ``pw-link``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess

log = logging.getLogger(__name__)

# Chrome's PipeWire node/port names (verified live).
CHROME_MIC = 'Google Chrome input'  # capture node; ports input_FL/FR
CHROME_OUT = 'Google Chrome'  # playback node; ports output_FL/FR

# Injected before any page script runs: Google Voice's getUserMedia turns on
# echo-cancellation / noise-suppression / auto-gain, which mangle injected
# audio (muffled, AGC "pumping"). Force them off.
MIC_CONSTRAINTS_JS = r"""
(function () {
  try {
    const md = navigator.mediaDevices;
    if (!md || !md.getUserMedia) return;
    const orig = md.getUserMedia.bind(md);
    md.getUserMedia = function (c) {
      c = c || {};
      if (c.audio) {
        const a = (typeof c.audio === 'object') ? c.audio : {};
        c.audio = Object.assign({}, a,
          {echoCancellation: false, noiseSuppression: false, autoGainControl: false});
      }
      return orig(c);
    };
  } catch (e) {}
})();
"""


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def create_null_sink(name: str) -> None:
    """Create a persistent null sink named ``name``."""
    _run(
        [
            'pw-cli', 'create-node', 'adapter',
            f'{{ factory.name=support.null-audio-sink node.name={name} '
            f'node.description={name} media.class=Audio/Sink '
            f'object.linger=true audio.position=[FL FR] }}',
        ]
    )


def destroy_sink(name: str) -> None:
    for line in _run(['wpctl', 'status']).splitlines():
        m = re.search(rf'(\d+)\.\s+{re.escape(name)}\b', line)
        if m:
            _run(['pw-cli', 'destroy', m.group(1)])


def _iter_links():
    """Yield ``(link_id, output_port, input_port)`` for every active link."""
    header = None
    for line in _run(['pw-link', '-I', '-l']).splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            header = line.strip()
            continue
        m = re.match(r'\s*(\d+)\s*\|(->|<-)\s*(.+)', line)
        if not m or header is None:
            continue
        lid, arrow, other = int(m.group(1)), m.group(2), m.group(3).strip()
        yield (lid, header, other) if arrow == '->' else (lid, other, header)


def _link(out_port: str, in_port: str) -> None:
    _run(['pw-link', out_port, in_port])


def _disconnect_into(in_node: str, *, keep_out_node: str | None = None) -> None:
    """Remove links feeding ``in_node``'s ports, except those from keep_out_node."""
    for lid, out_port, in_port in _iter_links():
        if in_port.startswith(in_node + ':') and (
            keep_out_node is None or not out_port.startswith(keep_out_node + ':')
        ):
            _run(['pw-link', '-d', str(lid)])


def _ports_exist(node: str) -> bool:
    listed = _run(['pw-link', '-o']).splitlines() + _run(['pw-link', '-i']).splitlines()
    return any(line.startswith(node + ':') for line in listed)


async def relink_mic(mic_sink: str, *, tries: int = 30) -> bool:
    """Feed ``mic_sink``'s monitor into Chrome's mic and drop the real mic.

    Chrome's capture node exists from call setup, so this is ready quickly and
    must happen before any injected audio.
    """
    for _ in range(tries):
        if _ports_exist(CHROME_MIC):
            for ch in ('FL', 'FR'):
                _link(f'{mic_sink}:monitor_{ch}', f'{CHROME_MIC}:input_{ch}')
            _disconnect_into(CHROME_MIC, keep_out_node=mic_sink)
            log.info('mic relinked: %s -> Chrome', mic_sink)
            return True
        await asyncio.sleep(0.5)
    log.warning('Chrome mic node never appeared; injected audio may not be sent')
    return False


async def relink_speaker(spk_sink: str, *, tries: int = 60) -> bool:
    """Tee Chrome's playback (the caller's voice) into ``spk_sink`` for capture.

    Chrome's playback node may not appear until the far end sends audio, so this
    retries for a while in the background.
    """
    for _ in range(tries):
        if _ports_exist(CHROME_OUT):
            for ch in ('FL', 'FR'):
                _link(f'{CHROME_OUT}:output_{ch}', f'{spk_sink}:playback_{ch}')
            log.info('speaker relinked: Chrome -> %s (capturing caller)', spk_sink)
            return True
        await asyncio.sleep(0.5)
    log.warning('Chrome playback node never appeared; not capturing caller audio')
    return False

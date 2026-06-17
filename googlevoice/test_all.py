import json
import os
import re

import pytest
import responses

from googlevoice import Credentials, Voice
from googlevoice import _browserlock as bl
from googlevoice.__main__ import main
from googlevoice.auth import API_BASE, load_session, sapisid_hash, save_session
from googlevoice.util import Message, Thread
from googlevoice.voice import Folder, _thread_id_for, normalize_number

FAKE_COOKIES = [
    {'name': 'SAPISID', 'value': 'sapisid-val', 'domain': '.google.com', 'path': '/'},
    {
        'name': '__Secure-1PAPISID',
        'value': '1p-val',
        'domain': '.google.com',
        'path': '/',
    },
    {
        'name': '__Secure-3PAPISID',
        'value': '3p-val',
        'domain': '.google.com',
        'path': '/',
    },
    {'name': 'SID', 'value': 'sid-val', 'domain': '.google.com', 'path': '/'},
]

ACCOUNT_RESPONSE = {'account': {'primaryDid': '+12085551234', 'phones': {}}}
THREADS_RESPONSE = {
    'thread': [
        {
            'id': 't.+12085550000',
            'read': False,
            'isText': True,
            'headingContactsPhoneNumberKey': ['+12085550000'],
            'item': [
                {
                    'id': 'abc123',
                    'startTime': '1781375718535',
                    'did': '+12085551234',
                    'contact': {'phoneNumber': '+12085550000', 'name': 'Pat'},
                    'type': 'smsIn',
                    # real SMS also carry a coarseType -- incoming must not be
                    # fooled by it (it is never 'callTypeIncoming' for SMS).
                    'coarseType': 'callTypeSmsIn',
                    'messageText': 'hello there',
                }
            ],
        }
    ]
}


# A thread holding a voicemail (with audio + transcript) and a missed call --
# exercises the type filters and download.
VOICEMAIL_THREAD = {
    'thread': [
        {
            'id': 't.+12085559999',
            'read': True,
            'isText': False,
            'headingContactsPhoneNumberKey': ['+12085559999'],
            'item': [
                {
                    'id': 'vm1',
                    'startTime': '1781375718535',
                    'did': '+12085551234',
                    'contact': {'phoneNumber': '+12085559999'},
                    'type': 'voicemail',
                    'messageText': 'hey call me back',
                    'recordingUrl': 'https://example.test/vm1.mp3',
                },
                {
                    'id': 'm1',
                    'startTime': '1781375710000',
                    'contact': {'phoneNumber': '+12085559999'},
                    'type': 'missed',
                    'coarseType': 'callTypeMissed',
                },
            ],
        }
    ]
}


@pytest.fixture
def voice():
    # auto_login off so a mocked 401 doesn't try to launch a real browser
    return Voice(credentials=Credentials(FAKE_COOKIES), auto_login=False)


class TestAuth:
    def test_sapisid_hash_format(self):
        value = sapisid_hash(FAKE_COOKIES, now=1_700_000_000)
        # All three labels present, each "<label> <ts>_<40-hex-sha1>".
        assert 'SAPISIDHASH 1700000000_' in value
        assert 'SAPISID1PHASH 1700000000_' in value
        assert 'SAPISID3PHASH 1700000000_' in value
        assert re.search(r'SAPISIDHASH \d+_[0-9a-f]{40}', value)

    def test_session_roundtrip(self, tmp_path):
        path = tmp_path / 'session.json'
        save_session(FAKE_COOKIES, path)
        assert load_session(path) == FAKE_COOKIES

    def test_requests_session_preserves_cookie_domain(self):
        jar = Credentials(FAKE_COOKIES).requests_session().cookies
        # Dotted domain must be preserved so cookies reach clients6.google.com.
        assert jar.get('SAPISID', domain='.google.com') == 'sapisid-val'


class TestVoiceReads:
    @responses.activate
    def test_number(self, voice):
        responses.post(API_BASE + 'account/get', json=ACCOUNT_RESPONSE)
        assert voice.number == '+12085551234'

    @responses.activate
    def test_threads_parsed(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        threads = voice.inbox()
        assert len(threads) == 1
        thread = threads[0]
        assert isinstance(thread, Thread)
        assert thread.id == 't.+12085550000'
        assert thread.read is False
        assert thread.contact == '+12085550000'
        assert thread.latest_text == 'hello there'
        assert thread.latest.incoming is True

    @responses.activate
    def test_thread_by_number(self, voice):
        single = {'thread': THREADS_RESPONSE['thread'][0]}
        responses.post(API_BASE + 'api2thread/get', json=single)
        found = voice.thread('+12085550000')
        assert found is not None
        assert found.id == 't.+12085550000'
        # request asks for the normalized thread id
        assert json.loads(responses.calls[-1].request.body)[0] == 't.+12085550000'

    @responses.activate
    def test_thread_not_found(self, voice):
        responses.post(API_BASE + 'api2thread/get', json={})  # no 'thread'
        assert voice.thread('+19998887777') is None

    @responses.activate
    def test_threads_messages_param(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        voice.threads(messages=100)
        body = json.loads(responses.calls[-1].request.body)
        assert body[2] == 100  # messages-per-thread slot

    @responses.activate
    def test_thread_accepts_formatted_number(self, voice):
        responses.post(API_BASE + 'api2thread/get', json={'thread': {'id': 't.x'}})
        assert voice.thread('(208) 555-0000') is not None
        # formatted number is normalized before becoming the thread id
        assert json.loads(responses.calls[-1].request.body)[0] == 't.+12085550000'

    @responses.activate
    def test_login_error_on_401(self, voice):
        from googlevoice.util import LoginError

        responses.post(API_BASE + 'account/get', status=401, body='nope')
        with pytest.raises(LoginError):
            voice.account()

    @responses.activate
    def test_auto_login_refreshes_and_retries(self, monkeypatch):
        import googlevoice.voice as voicemod

        # First account/get 401s, then (after a "refresh") succeeds.
        responses.post(API_BASE + 'account/get', status=401, body='nope')
        responses.post(API_BASE + 'account/get', json=ACCOUNT_RESPONSE)

        calls = []

        def fake_login(*a, **k):
            calls.append(1)
            return Credentials(FAKE_COOKIES)

        monkeypatch.setattr(voicemod.auth, 'browser_login', fake_login)
        v = Voice(credentials=Credentials(FAKE_COOKIES), auto_login=True)
        assert v.number == '+12085551234'
        assert calls == [1]  # refreshed exactly once


class TestSend:
    @responses.activate
    def test_send_sms_body_shape(self, voice):
        responses.post(API_BASE + 'api2thread/sendsms', json={})
        voice.send_sms('+12085550000', 'hi', recaptcha=['tok', None, None, 'tok2'])
        body = json.loads(responses.calls[-1].request.body)
        assert len(body) == 11
        assert body[4] == 'hi'
        assert body[5] == 't.+12085550000'
        assert isinstance(body[8], list) and len(body[8]) == 1  # [nonce]
        assert body[10] == ['tok', None, None, 'tok2']

    def test_thread_id_for(self):
        assert _thread_id_for('+12085550000') == 't.+12085550000'
        assert _thread_id_for('(208) 555-0000') == 't.+12085550000'  # normalized
        assert _thread_id_for('t.+12085550000') == 't.+12085550000'
        assert _thread_id_for('g.Group Message.x') == 'g.Group Message.x'
        assert _thread_id_for('c.PCIFABCDEF') == 'c.PCIFABCDEF'  # call id passthrough


class TestNumbers:
    def test_normalize_number(self):
        assert normalize_number('+1 (208) 555-0123') == '+12085550123'
        assert normalize_number('208-555-0123') == '+12085550123'
        assert normalize_number('2085550123') == '+12085550123'
        assert normalize_number('12085550123') == '+12085550123'
        assert normalize_number('+447911123456') == '+447911123456'  # intl untouched
        assert normalize_number('22000') == '22000'  # short code untouched
        assert normalize_number('t.+12085550123') == 't.+12085550123'  # id untouched


class TestMessage:
    def test_message_attributes(self):
        msg = Message(THREADS_RESPONSE['thread'][0]['item'][0])
        assert msg.text == 'hello there'
        assert msg.type == 'smsIn'
        assert msg.incoming is True
        assert msg.phone_number == '+12085550000'
        assert msg.start_time is not None

    def test_incoming_direction(self):
        # SMS: type is authoritative even though a (non-Incoming) coarseType
        # is present -- the bug this guards against.
        assert Message({'type': 'smsIn', 'coarseType': 'callTypeSmsIn'}).incoming
        assert not Message({'type': 'smsOut', 'coarseType': 'callTypeSmsOut'}).incoming
        # calls/voicemail: direction from coarseType
        assert not Message({'type': 'sip', 'coarseType': 'callTypeOutgoing'}).incoming
        assert Message({'type': 'sip', 'coarseType': 'callTypeIncoming'}).incoming
        assert Message({'type': 'missed', 'coarseType': 'callTypeMissed'}).incoming
        assert Message({'type': 'voicemail', 'recordingUrl': 'x'}).incoming

    def test_as_dict(self):
        thread = Thread(None, THREADS_RESPONSE['thread'][0])
        d = thread.as_dict()
        assert d['id'] == 't.+12085550000'
        assert d['contact'] == '+12085550000'
        assert d['read'] is False
        msg = d['messages'][0]
        assert msg['text'] == 'hello there'
        assert msg['incoming'] is True
        # start_time is ISO-8601 with a UTC offset
        assert msg['start_time'].endswith('+00:00')


class TestFolders:
    @responses.activate
    def test_folder_selectors(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        for call, expected in [
            (voice.calls, Folder.CALLS),
            (voice.inbox, Folder.INBOX),
            (voice.spam, Folder.SPAM),
            (voice.archived, Folder.ARCHIVE),
        ]:
            call()
            assert json.loads(responses.calls[-1].request.body)[0] == expected


class TestTypeFilters:
    @responses.activate
    def test_voicemails(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=VOICEMAIL_THREAD)
        vms = voice.voicemails()
        assert len(vms) == 1
        assert vms[0].is_voicemail
        assert vms[0].text == 'hey call me back'
        assert vms[0].has_audio
        assert vms[0].recording_url == 'https://example.test/vm1.mp3'
        # voicemails come from the Voicemail folder
        assert json.loads(responses.calls[0].request.body)[0] == Folder.VOICEMAIL

    @responses.activate
    def test_missed(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=VOICEMAIL_THREAD)
        missed = voice.missed()
        assert len(missed) == 1
        assert missed[0].type == 'missed'
        assert json.loads(responses.calls[0].request.body)[0] == Folder.CALLS


class TestThreadActions:
    """thread/batchupdateattributes -- body [[[values, mask, 1]]]."""

    @responses.activate
    def test_archive_sets_index_5(self, voice):
        responses.post(API_BASE + 'thread/batchupdateattributes', json={})
        voice.archive('+12085550000')
        body = json.loads(responses.calls[-1].request.body)
        values, mask, tail = body[0][0]
        assert values == ['t.+12085550000', None, None, None, None, 1]
        assert mask == [None, None, None, None, None, 1]
        assert tail == 1

    @responses.activate
    def test_unarchive_sets_index_5_to_zero(self, voice):
        responses.post(API_BASE + 'thread/batchupdateattributes', json={})
        voice.unarchive('+12085550000')
        values = json.loads(responses.calls[-1].request.body)[0][0][0]
        assert values == ['t.+12085550000', None, None, None, None, 0]

    @responses.activate
    def test_spam_block_read_indices(self, voice):
        responses.post(API_BASE + 'thread/batchupdateattributes', json={})
        for action, index in [
            (voice.mark_spam, 2),
            (voice.block, 1),
            (voice.mark_read, 3),
        ]:
            action('+12085550000')
            values = json.loads(responses.calls[-1].request.body)[0][0][0]
            assert values[index] == 1 and len(values) == index + 1

    @responses.activate
    def test_thread_shortcut_calls_voice(self, voice):
        responses.post(API_BASE + 'thread/batchupdateattributes', json={})
        thread = Thread(voice, THREADS_RESPONSE['thread'][0])
        thread.mark_spam()
        values = json.loads(responses.calls[-1].request.body)[0][0][0]
        assert values == ['t.+12085550000', None, 1]  # index 2 = spam

    def test_delete_not_implemented(self, voice):
        with pytest.raises(NotImplementedError):
            voice.delete('+12085550000')


class TestDownload:
    @responses.activate
    def test_download_saves_audio(self, voice, tmp_path):
        responses.post(API_BASE + 'api2thread/list', json=VOICEMAIL_THREAD)
        responses.get('https://example.test/vm1.mp3', body=b'ID3audio')
        vm = voice.voicemails()[0]
        path = vm.download(str(tmp_path))
        assert path.endswith('vm1.mp3')
        assert (tmp_path / 'vm1.mp3').read_bytes() == b'ID3audio'

    @responses.activate
    def test_download_without_audio_raises(self, voice):
        from googlevoice.util import DownloadError

        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        msg = voice.inbox()[0].latest  # an SMS, no audio
        with pytest.raises(DownloadError):
            msg.download()


class TestSearch:
    @responses.activate
    def test_search_body_and_parse(self, voice):
        responses.post(API_BASE + 'api2thread/search', json=THREADS_RESPONSE)
        results = voice.search('hello', count=50)
        assert len(results) == 1
        body = json.loads(responses.calls[-1].request.body)
        assert body[0] == 'hello' and body[1] == 50


class TestProfileLock:
    @pytest.mark.skipif(bl.fcntl is None, reason='POSIX advisory locks only')
    def test_busy_profile_raises(self, tmp_path):
        held = bl.ProfileLock(tmp_path)
        held.acquire()
        try:
            with pytest.raises(bl.BrowserBusyError) as exc:
                bl.ProfileLock(tmp_path).acquire()  # same profile, already locked
            assert str(os.getpid()) in str(exc.value)  # error names the holder
        finally:
            held.release()
        # released -> can acquire again
        again = bl.ProfileLock(tmp_path)
        again.acquire()
        again.release()

    @pytest.mark.skipif(os.name != 'posix', reason='symlink / os.kill semantics')
    def test_clears_stale_singleton(self, tmp_path):
        # Chrome's lock owned by a dead pid should be removed.
        (tmp_path / 'SingletonLock').symlink_to(f'somehost-{0x7FFFFFFF}')
        assert os.path.lexists(tmp_path / 'SingletonLock')
        bl.clear_stale_singleton(tmp_path)
        assert not os.path.lexists(tmp_path / 'SingletonLock')


class TestCLI:
    @responses.activate
    def test_inbox_json(self, monkeypatch, capsys):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        monkeypatch.setattr(
            'googlevoice.__main__.Voice',
            lambda: Voice(credentials=Credentials(FAKE_COOKIES)),
        )
        main(['inbox', '--json'])
        data = json.loads(capsys.readouterr().out)
        assert data[0]['id'] == 't.+12085550000'
        assert data[0]['messages'][0]['text'] == 'hello there'

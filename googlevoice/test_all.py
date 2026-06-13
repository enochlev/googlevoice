import json
import re

import pytest
import responses

from googlevoice import Credentials, Voice
from googlevoice.__main__ import main
from googlevoice.auth import API_BASE, load_session, sapisid_hash, save_session
from googlevoice.util import Message, Thread
from googlevoice.voice import _thread_id_for, normalize_number

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
                    'messageText': 'hello there',
                }
            ],
        }
    ]
}


@pytest.fixture
def voice():
    return Voice(credentials=Credentials(FAKE_COOKIES))


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
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        found = voice.thread('+12085550000')
        assert found is not None
        assert found.id == 't.+12085550000'
        assert voice.thread('+19998887777') is None  # not in the list

    @responses.activate
    def test_threads_messages_param(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        voice.threads(messages=100)
        body = json.loads(responses.calls[-1].request.body)
        assert body[2] == 100  # messages-per-thread slot

    @responses.activate
    def test_thread_accepts_formatted_number(self, voice):
        responses.post(API_BASE + 'api2thread/list', json=THREADS_RESPONSE)
        # thread id in the fixture is t.+12085550000
        assert voice.thread('(208) 555-0000') is not None

    @responses.activate
    def test_login_error_on_401(self, voice):
        from googlevoice.util import LoginError

        responses.post(API_BASE + 'account/get', status=401, body='nope')
        with pytest.raises(LoginError):
            voice.account()


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

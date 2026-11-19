#!/usr/bin/env python3

import base64
import http.server
import json
import os
import shutil
import threading
import time
import unittest
from unittest import mock

from passlib.apache import HtpasswdFile

from smstools_http_api import create_app, hooks_cli, webhooks

BASE = 'tmp/test'
AUTH = {'Authorization': 'Basic ' + base64.b64encode(b'test:test').decode()}


class AppTestCase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(BASE, ignore_errors=True)
        for kind in ('incoming', 'outgoing', 'checked', 'failed', 'sent'):
            os.makedirs(os.path.join(BASE, kind))
        ht = HtpasswdFile(os.path.join(BASE, 'htpasswd.users'), new=True)
        ht.set_password('test', 'test')
        ht.save()
        self.app = create_app('test')
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(BASE, ignore_errors=True)

    def send(self, body):
        return self.client.post('/api/v1.0/sms/outgoing', headers=AUTH, data=json.dumps(body),
                                content_type='application/json')

    def test_app_configuration(self):
        self.assertTrue(self.app.config['TESTING'])

    def test_unauthorized_access(self):
        response = self.client.get('/api/v1.0/sms/sent/test')
        self.assertEqual(response.status_code, 401)
        self.assertIn('Unauthorized access', response.get_data(as_text=True))

    def test_wrong_password(self):
        bad = {'Authorization': 'Basic ' + base64.b64encode(b'test:nope').decode()}
        self.assertEqual(self.client.get('/api/v1.0/sms/sent/', headers=bad).status_code, 401)

    def test_authorized_not_found(self):
        response = self.client.get('/api/v1.0/sms/sent/test', headers=AUTH)
        self.assertEqual(response.status_code, 404)

    def test_send_and_read_back(self):
        response = self.send({'mobiles': ['436761234567'], 'text': 'Hi, Jack!'})
        self.assertEqual(response.status_code, 201)
        result = response.get_json()
        entry = result['mobiles']['436761234567']
        self.assertEqual(entry['response'], 'Ok')
        self.assertFalse(result['demo'])
        spooled = os.path.join(BASE, 'outgoing', entry['message_id'])
        with open(spooled, 'rb') as fp:
            content = fp.read().decode()
        self.assertIn('To: 436761234567', content)
        self.assertIn('Hi, Jack!', content)
        got = self.client.get('/api/v1.0/sms/outgoing/' + entry['message_id'], headers=AUTH).get_json()
        self.assertEqual(got['To'], '436761234567')
        self.assertEqual(got['text'], 'Hi, Jack!')

    def test_unicode_text(self):
        result = self.send({'mobiles': ['436761234567'], 'text': 'Grüße'}).get_json()
        mid = result['mobiles']['436761234567']['message_id']
        got = self.client.get('/api/v1.0/sms/outgoing/' + mid, headers=AUTH).get_json()
        self.assertEqual(got['Alphabet'], 'UCS2')
        self.assertEqual(got['text'], 'Grüße')

    def test_demo_mode(self):
        self.app.config['DEMO'] = True
        result = self.send({'mobiles': ['436761234567'], 'text': 'code 123'}).get_json()
        self.assertTrue(result['demo'])
        mid = result['mobiles']['436761234567']['message_id']
        self.assertEqual(os.listdir(os.path.join(BASE, 'outgoing')), [])
        got = self.client.get('/api/v1.0/sms/sent/' + mid, headers=AUTH).get_json()
        self.assertEqual(got['Demo'], 'yes')

    def test_get_query(self):
        response = self.client.get('/api/v1.0/sms/outgoing?mobiles=+436761234567&text=hi', headers=AUTH)
        self.assertEqual(response.status_code, 201)
        self.assertIn('+436761234567', response.get_json()['mobiles'])

    def test_invalid_input(self):
        self.assertEqual(self.send({'text': 'x'}).status_code, 400)
        self.assertEqual(self.send({'mobiles': [], 'text': 'x'}).status_code, 400)
        response = self.client.post('/api/v1.0/sms/outgoing', headers=AUTH, data='nope')
        self.assertEqual(response.status_code, 400)
        result = self.send({'mobiles': ['12ab'], 'text': 'x'}).get_json()
        self.assertTrue(result['mobiles']['12ab']['response'].startswith('Failed'))

    def test_list(self):
        self.send({'mobiles': ['436761234567'], 'text': 'x'})
        result = self.client.get('/api/v1.0/sms/outgoing/', headers=AUTH).get_json()
        self.assertEqual(result['total_count'], 1)

    def test_path_traversal(self):
        for mid in ('..%2Fhtpasswd.users', '.hidden', 'x.LOCK'):
            response = self.client.get('/api/v1.0/sms/sent/' + mid, headers=AUTH)
            self.assertEqual(response.status_code, 404, mid)

    def test_monitoring(self):
        self.assertEqual(self.client.get('/api/v1.0/monitoring').get_json()['monitoring'], 'ok')


class Recorder(http.server.BaseHTTPRequestHandler):
    """Webhook target: records POSTs, answers with the status in .status."""
    status = 200
    requests = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        Recorder.requests.append((self.path, dict(self.headers), json.loads(body)))
        self.send_response(Recorder.status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"result": true}')

    def log_message(self, *args):
        pass


class WebhookTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(('127.0.0.1', 0), Recorder)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:%d' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        shutil.rmtree(BASE, ignore_errors=True)
        os.makedirs(os.path.join(BASE, 'incoming'))
        Recorder.status, Recorder.requests = 200, []
        self.config = dict(create_app('test').config)
        self.config['WEBHOOKS'] = [{'name': 'a', 'url': self.url + '/a', 'headers': {'X-Token': 't'}}]
        self.sms = os.path.join(BASE, 'incoming', 'GSM1.abc')
        with open(self.sms, 'wb') as fp:
            fp.write(b'From: 436761234567\nSent: 26-11-19 11:29:14\nAlphabet: ISO\n\nTest 3')

    def tearDown(self):
        shutil.rmtree(BASE, ignore_errors=True)

    def queued(self):
        qdir = os.path.join(BASE, 'webhooks')
        return sorted(n for n in os.listdir(qdir) if n.endswith('.json')) if os.path.isdir(qdir) else []

    def test_received_is_delivered(self):
        webhooks.enqueue(self.config, 'RECEIVED', self.sms)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(webhooks.deliver_pending(self.config), {'done': 1})
        self.assertEqual(self.queued(), [])
        path, headers, event = Recorder.requests[0]
        self.assertEqual(path, '/a')
        self.assertEqual(headers['X-Token'], 't')
        self.assertEqual(event['event'], 'RECEIVED')
        self.assertEqual(event['from'], '436761234567')
        self.assertEqual(event['text'], 'Test 3')
        self.assertEqual(event['message_id'], 'GSM1.abc')
        self.assertEqual(event['kind'], 'incoming')

    def test_unsubscribed_event_not_queued(self):
        self.assertIsNone(webhooks.enqueue(self.config, 'SENT', self.sms))
        self.assertEqual(self.queued(), [])

    def test_retry_then_success(self):
        webhooks.enqueue(self.config, 'RECEIVED', self.sms)
        Recorder.status = 503
        self.assertEqual(webhooks.deliver_pending(self.config), {'retry': 1})
        # not due yet
        self.assertEqual(webhooks.deliver_pending(self.config), {'wait': 1})
        Recorder.status = 200
        self.assertEqual(webhooks.deliver_pending(self.config, now=time.time() + 60), {'done': 1})
        self.assertEqual(len(Recorder.requests), 2)

    def test_gives_up_after_max_age(self):
        webhooks.enqueue(self.config, 'RECEIVED', self.sms)
        Recorder.status = 500
        self.config['WEBHOOK_MAX_AGE'] = 10
        self.assertEqual(webhooks.deliver_pending(self.config, now=time.time() + 20), {'failed': 1})
        self.assertEqual(len(os.listdir(os.path.join(BASE, 'webhooks', 'failed'))), 1)

    def test_only_failed_hook_is_retried(self):
        self.config['WEBHOOKS'].append({'name': 'b', 'url': 'http://127.0.0.1:1/unreachable', 'timeout': 2})
        webhooks.enqueue(self.config, 'RECEIVED', self.sms)
        self.assertEqual(webhooks.deliver_pending(self.config), {'retry': 1})
        self.config['WEBHOOKS'][1]['url'] = self.url + '/b'
        self.assertEqual(webhooks.deliver_pending(self.config, now=time.time() + 60), {'done': 1})
        self.assertEqual([r[0] for r in Recorder.requests], ['/a', '/b'])

    def test_payload_filter_check(self):
        self.config['WEBHOOKS'] = [
            {'name': 'zabbix', 'url': self.url + '/z',
             'payload': lambda e: {'method': 'history.push', 'params': [{'value': e['text']}]},
             'check': lambda status, body: json.loads(body).get('result') is True},
            {'name': 'skip', 'url': self.url + '/s', 'filter': lambda e: e['from'] == 'nobody'},
        ]
        webhooks.enqueue(self.config, 'RECEIVED', self.sms)
        self.assertEqual(webhooks.deliver_pending(self.config), {'done': 1})
        self.assertEqual(len(Recorder.requests), 1)
        self.assertEqual(Recorder.requests[0][2], {'method': 'history.push', 'params': [{'value': 'Test 3'}]})

    def test_email_hook(self):
        self.config['WEBHOOKS'] = [{'name': 'mail', 'type': 'email', 'to': 'me@example.org',
                                    'from': 'SMS <sms@example.org>', 'smtp_host': 'mx.example.org', 'smtp_port': 26}]
        with mock.patch('smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            webhooks.enqueue(self.config, 'RECEIVED', self.sms)
            self.assertEqual(webhooks.deliver_pending(self.config), {'done': 1})
        smtp.assert_called_once_with('mx.example.org', 26, timeout=30)
        msg = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        self.assertEqual(msg['To'], 'me@example.org')
        self.assertEqual(msg['Subject'], 'SMS from 436761234567')
        self.assertIn('@example.org>', msg['Message-ID'])
        self.assertTrue(msg.get_content().startswith('Test 3\n'))
        self.assertIn('Sent: 26-11-19 11:29:14', msg.get_content())

    def test_email_hook_retries(self):
        self.config['WEBHOOKS'] = [{'name': 'mail', 'type': 'email', 'to': ['me@example.org']}]
        with mock.patch('smtplib.SMTP', side_effect=ConnectionRefusedError(111, 'refused')):
            webhooks.enqueue(self.config, 'RECEIVED', self.sms)
            self.assertEqual(webhooks.deliver_pending(self.config), {'retry': 1})

    def test_eventhandler_unreadable_settings(self):
        # Debian runs smsd as smsd:dialout, the settings file is root:smsd 640
        real = hooks_cli.load_config
        calls = []

        def fake(name, settings):
            calls.append(settings)
            if settings is None:
                raise PermissionError(13, 'Permission denied')
            return dict(real('test', settings), WEBHOOKS=[])

        with mock.patch.object(hooks_cli, 'load_config', fake):
            self.assertEqual(hooks_cli.event_main(['RECEIVED', self.sms]), 0)
        self.assertEqual(calls, [None, os.devnull])
        self.assertEqual(len(self.queued()), 1)
        # the worker has the settings: a subscribed hook gets it ...
        self.assertEqual(webhooks.deliver_pending(self.config), {'done': 1})
        self.assertEqual(len(Recorder.requests), 1)
        # ... an event nobody subscribed to is dropped
        with mock.patch.object(hooks_cli, 'load_config', fake):
            hooks_cli.event_main(['SENT', self.sms])
        self.assertEqual(webhooks.deliver_pending(self.config), {'done': 1})
        self.assertEqual(len(Recorder.requests), 1)

    def test_eventhandler_command(self):
        settings = os.path.join(BASE, 'settings.py')
        with open(settings, 'w') as fp:
            fp.write('WEBHOOKS = [{"name": "a", "url": %r, "events": ["RECEIVED", "SENT"]}]\n' % (self.url + '/a'))
        env = dict(os.environ, FLASK_CONFIG='test', SMSTOOLS_HTTP_API_SETTINGS=settings)
        old = os.environ.copy()
        os.environ.update(env)
        try:
            self.assertEqual(hooks_cli.event_main(['RECEIVED', self.sms]), 0)
            self.assertEqual(hooks_cli.event_main(['CALL', self.sms]), 0)
            self.assertEqual(hooks_cli.event_main([]), 2)
            self.assertEqual(len(self.queued()), 1)
            self.assertEqual(hooks_cli.worker_main(['-c', 'development', '--settings', settings, '--once']), 0)
        finally:
            os.environ.clear()
            os.environ.update(old)


VCARDS = '''BEGIN:VCARD
VERSION:3.0
UID:a
FN:Lydia Fetka
N:Fetka;Lydia;;;
NICKNAME:mum,mama
CATEGORIES:eltern,familie
EMAIL;TYPE=HOME:lydia@example.org
EMAIL;TYPE=OTHER:lydia@gmail.example
TEL;TYPE=CELL:+43 664 1111111
END:VCARD
BEGIN:VCARD
VERSION:3.0
UID:b
FN:Josef Fetka
N:Fetka;Josef;;;
NICKNAME:dad,papa
CATEGORIES:eltern,familie
EMAIL;TYPE=HOME:josef@example.org
TEL;TYPE=CELL:0681 2222222
END:VCARD
BEGIN:VCARD
VERSION:3.0
UID:c
FN:Mario Fetka
N:Fetka;Mario;;;
NICKNAME:mario
CATEGORIES:buben,familie
EMAIL:mario@example.org
TEL;TYPE=CELL:+436763333333
END:VCARD
'''


class ContactsTestCase(unittest.TestCase):
    def setUp(self):
        from smstools_http_api import contacts
        self.book = contacts.Directory(contacts.parse_vcards(VCARDS))

    def test_resolve(self):
        self.assertEqual([c['given'] for c in self.book.resolve('MUM')], ['Lydia'])
        self.assertEqual([c['given'] for c in self.book.resolve('eltern')], ['Lydia', 'Josef'])
        self.assertEqual([c['given'] for c in self.book.resolve('lydia.fetka')], ['Lydia'])
        self.assertEqual(self.book.resolve('nobody'), [])
        self.assertEqual(self.book.numbers(self.book.resolve('dad')[0]), ['436812222222'])
        self.assertEqual(self.book.by_number('+436641111111')[0]['given'], 'Lydia')
        self.assertEqual(self.book.by_email('Lydia@GMAIL.example')[0]['given'], 'Lydia')

    def test_folded_and_escaped(self):
        from smstools_http_api import contacts
        cards = contacts.parse_vcards('BEGIN:VCARD\r\nFN:A\\, B\r\nNICKNAME:x\\,y,z\r\n'
                                      'item1.EMAIL;PREF=1:a@ex\r\n ample.org\r\nEND:VCARD\r\n')
        self.assertEqual(cards[0]['name'], 'A, B')
        self.assertEqual(cards[0]['nicknames'], ['x,y', 'z'])
        self.assertEqual(cards[0]['emails'], ['a@example.org'])

    def test_tokens(self):
        self.assertEqual(webhooks.parse_tokens('@Mum, @rene: Hallo @x'), (['mum', 'rene'], 'Hallo @x'))
        self.assertEqual(webhooks.parse_tokens('Hallo'), ([], 'Hallo'))


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(BASE, ignore_errors=True)
        os.makedirs(os.path.join(BASE, 'incoming'))
        with open(os.path.join(BASE, 'contacts.vcf'), 'w') as fp:
            fp.write(VCARDS)
        self.config = dict(create_app('test').config)
        self.config['CONTACTS'] = {'file': os.path.join(BASE, 'contacts.vcf')}
        self.config['WEBHOOKS'] = [{'name': 'route', 'type': 'route', 'domain': 'sms.example.org',
                                    'default_to': 'mario@example.org', 'skip': ['zabbix'],
                                    'mail_domains': ['example.org']}]

    def tearDown(self):
        shutil.rmtree(BASE, ignore_errors=True)

    def deliver(self, text, sender='436641111111'):
        path = os.path.join(BASE, 'incoming', 'GSM1.x')
        with open(path, 'wb') as fp:
            fp.write(('From: %s\nReceived: 26-11-19 12:00:00\n\n%s' % (sender, text)).encode())
        with mock.patch('smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            webhooks.enqueue(self.config, 'RECEIVED', path)
            self.assertEqual(webhooks.deliver_pending(self.config), {'done': 1})
        calls = smtp.return_value.__enter__.return_value.send_message.call_args_list
        return [c[0][0] for c in calls]

    def test_tokens_route_to_contacts(self):
        msg, = self.deliver('@eltern @mario Essen um 6')
        self.assertEqual(msg['To'], 'lydia@example.org, josef@example.org, mario@example.org')
        self.assertEqual(msg['From'], '"Lydia Fetka (SMS)" <436641111111@sms.example.org>')
        self.assertEqual(msg['Subject'], 'SMS from Lydia Fetka')
        self.assertTrue(msg.get_content().startswith('Essen um 6\n'))

    def test_no_token_goes_to_default(self):
        msg, = self.deliver('Hallo', sender='4369912345')
        self.assertEqual(msg['To'], 'mario@example.org')
        self.assertEqual(msg['Subject'], 'SMS from +4369912345')

    def test_unknown_token_goes_to_default(self):
        msg, = self.deliver('@nobody hi')
        self.assertEqual(msg['To'], 'mario@example.org')
        self.assertIn('@nobody', msg.get_content())

    def test_skipped_token_left_to_other_hook(self):
        self.assertEqual(self.deliver('@zabbix ack 42'), [])


class Mail2smsTestCase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(BASE, ignore_errors=True)
        for kind in ('outgoing', 'sent'):
            os.makedirs(os.path.join(BASE, kind))
        with open(os.path.join(BASE, 'contacts.vcf'), 'w') as fp:
            fp.write(VCARDS)
        self.settings = os.path.join(BASE, 'settings.py')
        with open(self.settings, 'w') as fp:
            fp.write('CONTACTS = {"file": %r}\nMAIL2SMS_SENDER_DOMAINS = ["example.org"]\n'
                     'MAIL2SMS_SENDERS = ["robot@other.example"]\n' % os.path.join(BASE, 'contacts.vcf'))

    def tearDown(self):
        shutil.rmtree(BASE, ignore_errors=True)

    def run_mail(self, sender, rcpt, mail):
        from smstools_http_api import mail2sms
        stdin = mock.Mock()
        stdin.buffer = __import__('io').BytesIO(mail.encode())
        with mock.patch('sys.stdin', stdin), mock.patch('sys.stdout', __import__('io').StringIO()):
            return mail2sms.main(['-c', 'test', '--settings', self.settings, sender, rcpt])

    def spooled(self):
        from smstools_http_api.message import read_message
        out = os.path.join(BASE, 'outgoing')
        return sorted((m['To'], m['text']) for m in (read_message(os.path.join(out, n)) for n in os.listdir(out)))

    def test_reply_to_number(self):
        mail = ('From: mario@example.org\nSubject: Re: SMS\nContent-Type: text/plain; charset=utf-8\n\n'
                'Bin unterwegs\n\nAm 19.11.2026 um 12:00 schrieb Lydia:\n> Essen um 6\n')
        self.assertEqual(self.run_mail('mario@example.org', '436641111111@sms.example.org', mail), 0)
        self.assertEqual(self.spooled(), [('436641111111', 'Mario: Bin unterwegs')])

    def test_group_skips_sender(self):
        mail = 'Subject: Kommt ihr?\n\n'
        self.assertEqual(self.run_mail('lydia@example.org', 'familie@sms.example.org', mail), 0)
        self.assertEqual(self.spooled(), [('436763333333', 'Lydia: Kommt ihr?'),
                                          ('436812222222', 'Lydia: Kommt ihr?')])

    def test_refused_and_unknown(self):
        mail = 'Subject: x\n\nhi\n'
        self.assertEqual(self.run_mail('lydia@gmail.example', 'mum@sms.example.org', mail), 77)
        self.assertEqual(self.run_mail('evil@example.org', 'mum@sms.example.org', mail), 77)
        self.assertEqual(self.run_mail('mario@example.org', 'nobody@sms.example.org', mail), 67)
        self.assertEqual(self.run_mail('robot@other.example', 'mum@sms.example.org', mail), 0)
        self.assertEqual(self.spooled(), [('436641111111', 'hi')])

    def test_spf_for_untrusted_clients(self):
        from smstools_http_api import mail2sms
        with open(self.settings, 'a') as fp:
            fp.write('MAIL2SMS_SENDER_DOMAINS = []\nMAIL2SMS_TRUSTED_CLIENTS = ["10.0.0.10"]\n')
        base = 'From: lydia@gmail.example\nSubject: hi\n\n'

        def run(client, headers=''):
            stdin = mock.Mock()
            stdin.buffer = __import__('io').BytesIO((headers + base).encode())
            with mock.patch('sys.stdin', stdin), mock.patch('sys.stdout', __import__('io').StringIO()):
                return mail2sms.main(['-c', 'test', '--settings', self.settings, '--client', client,
                                      'lydia@gmail.example', 'dad@sms.example.org'])

        self.assertEqual(run('10.0.0.10'), 0)
        self.assertEqual(run('10.0.0.9'), 77)
        self.assertEqual(run('10.0.0.9', 'Received-SPF: softfail (x)\nReceived-SPF: pass (forged)\n'), 77)
        self.assertEqual(run('10.0.0.9', 'Received-SPF: Pass (gmail.example: ok)\n'), 0)
        self.assertEqual(len(self.spooled()), 2)

    def test_spf_only_from_the_gateway(self):
        from smstools_http_api import mail2sms
        gw = 'mailgw.example.org'
        entry = 'Received: from mail-x.gmail.example (mail-x.gmail.example [192.0.2.1])\n by %s (Proxmox) with ESMTPS id 1\n' % gw
        local = 'Received: from %s (localhost [127.0.0.1])\n by %s (Proxmox) with ESMTP id 2\n' % (gw, gw)
        ours = 'Received-SPF: pass (gmail.example: 192.0.2.1 is authorized) receiver=%s; identity=mailfrom\n' % gw
        forged = 'Received-SPF: pass (forged) receiver=%s; identity=mailfrom\n' % gw
        check = lambda headers: mail2sms.spf_pass(email.message_from_string(headers + 'Subject: x\n\nhi\n'), gw)
        import email
        self.assertTrue(check(local + ours + entry))
        self.assertFalse(check(local + entry + forged))                  # sender wrote it
        self.assertFalse(check(local + ours.replace('pass', 'softfail') + entry + forged))
        self.assertFalse(check(local + ours.replace(gw, 'other.example') + entry))
        self.assertFalse(check(local + ours.replace(gw, gw + '.evil.example') + entry))
        self.assertTrue(mail2sms.spf_pass(email.message_from_string(ours + '\nhi'), None))

    def test_long_unicode_text_is_cut(self):
        mail = 'Content-Type: text/plain; charset=utf-8\n\n' + 'ä' * 500 + '\n-- \nSignatur\n'
        self.assertEqual(self.run_mail('mario@example.org', 'dad@sms.example.org', mail), 0)
        (_, text), = self.spooled()
        self.assertEqual(len(text), 3 * 67)
        self.assertTrue(text.endswith('…'))


if __name__ == '__main__':
    unittest.main()

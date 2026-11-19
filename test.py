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


if __name__ == '__main__':
    unittest.main()

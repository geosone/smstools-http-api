#!/usr/bin/env python3

import base64
import json
import os
import shutil
import unittest

from passlib.apache import HtpasswdFile

from app import create_app

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


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3

import os
import re
import uuid
from email.message import Message

from flask import current_app, jsonify, request

from ..message import read_message
from .authentication import username
from .errors import forbidden, not_found

# Spooler file names: the API uses UUIDs, smsd its own names; never a path.
_MESSAGE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def spool_path(kind, message_id):
    if kind not in current_app.config['KINDS'] or not _MESSAGE_ID.match(message_id) \
            or message_id.endswith('.LOCK'):
        return None
    return os.path.join(current_app.config[kind.upper()], message_id)

def access_mobile(mobile):
    if not 'USER_WHITELIST' in current_app.config.keys():
        # Number access control disabled.
        return True
    if current_app.config['USER_WHITELIST'].get(username()):
        return mobile in current_app.config['USER_WHITELIST'].get(username(), [])
    else:
        return True

def validate_mobile(mobile):
    return re.match(r'^\+?\d+$', mobile) is not None

def is_admin(user):
    return user in current_app.config.get('ADMIN_ACCOUNTS', [])

def list_some_sms(kind):
    if kind not in current_app.config['KINDS']:
        return not_found(None)

    limit = current_app.config.get('LIMIT') or False
    message_ids = os.listdir(current_app.config[kind.upper()])
    message_ids = [mid for mid in message_ids if not mid.endswith('.LOCK')]
    total_count = len(message_ids)
    result = {}
    result['total_count'] = total_count
    result['limit'] = limit
    result['message_id'] = message_ids[:limit] if limit else message_ids

    return jsonify(result)

def delete_some_sms(kind, message_id):
    path = spool_path(kind, message_id)
    if path is None:
        return not_found(None)

    if is_admin(username()):
        result = {}
        try:
            os.remove(path)
            result['deleted'] = kind + '/' + message_id
            return jsonify(result)
        except OSError:
            return not_found(None)

    return forbidden(None)

def get_some_sms(kind, message_id):
    path = spool_path(kind, message_id)
    if path is None:
        return not_found(None)
    try:
        result = read_message(path)
    except EnvironmentError:
        return not_found(None)

    if result.get('From') == username() or is_admin(username()):
        return jsonify(result)
    return forbidden(None)

def detect_coding(text):
    text_len=len(text)
    try:
        parts_count = text_len // 153 + (text_len % 153 > 0)
        text = text.encode('ascii')
        coding = 'ISO'
    except UnicodeEncodeError:
        parts_count = text_len // 67 + (text_len % 67 > 0)
        text = text.encode('utf-16-be')
        coding = 'UCS2'

    return text, coding, parts_count

def send_sms(data):
    text, coding, parts_count = detect_coding(data['text'])
    demo = bool(current_app.config.get('DEMO'))

    result = {
        'demo': demo,
        'sent_text': data['text'],
        'parts_count': parts_count,
        'mobiles': {}
    }

    for mobile in data['mobiles']:
        # generate message_id
        message_id = str(uuid.uuid4())

        result['mobiles'][mobile] = {}
        result['mobiles'][mobile]['message_id'] = message_id
        result['mobiles'][mobile]['dlr_status'] = os.path.join(os.path.dirname(request.url),
                                                  os.path.basename(current_app.config['SENT']), message_id)

        if not validate_mobile(mobile):
            current_app.logger.info('Message from [%s] to [%s] have invalid mobile number' % (username(), mobile))
            result['mobiles'][mobile]['response'] = 'Failed: invalid mobile number'
            continue

        if access_mobile(mobile):
            # DEMO: nothing goes to smsd; the message lands in the "sent" spool
            # (readable through dlr_status) marked "Demo: yes".
            spool = current_app.config['SENT' if demo else 'OUTGOING']
            lock_file = os.path.join(spool, message_id + '.LOCK')
            m = Message()
            m.add_header('From', username())
            m.add_header('To', mobile)
            m.add_header('Alphabet', coding)
            if demo:
                m.add_header('Demo', 'yes')
            if data.get('queue'):
                result.update({'queue' : data['queue']})
                m.add_header('Queue', result.get('queue'))
            m.set_payload(text)

            with open(lock_file, 'wb') as fp:
                fp.write(m.as_bytes())

            msg_file = lock_file.split('.LOCK')[0]
            os.rename(lock_file, msg_file)
            os.chmod(msg_file, 0o660)
            if demo:
                current_app.logger.info('DEMO: message from [%s] to [%s] NOT sent, stored as [%s]: %s'
                                        % (username(), mobile, msg_file, data['text']))
            else:
                current_app.logger.info('Message from [%s] to [%s] placed to the spooler as [%s]'
                                        % (username(), mobile, msg_file))
            result['mobiles'][mobile]['response'] = 'Ok'
        else:
            current_app.logger.info('Message from [%s] to [%s] have forbidden mobile number' % (username(), mobile))
            result['mobiles'][mobile]['response'] = 'Failed: forbidden mobile number'

    return result

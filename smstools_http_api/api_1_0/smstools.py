#!/usr/bin/env python3

import os
import re

from flask import current_app, jsonify, request

from ..message import encode_text, read_message, write_outgoing
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

def send_sms(data):
    _, _, parts_count = encode_text(data['text'])
    demo = bool(current_app.config.get('DEMO'))

    result = {
        'demo': demo,
        'sent_text': data['text'],
        'parts_count': parts_count,
        'mobiles': {}
    }

    if data.get('queue'):
        result['queue'] = data['queue']

    for mobile in data['mobiles']:
        result['mobiles'][mobile] = {}

        if not validate_mobile(mobile):
            current_app.logger.info('Message from [%s] to [%s] have invalid mobile number' % (username(), mobile))
            result['mobiles'][mobile]['response'] = 'Failed: invalid mobile number'
            continue

        if access_mobile(mobile):
            # DEMO: nothing goes to smsd; the message lands in the "sent" spool
            # (readable through dlr_status) marked "Demo: yes".
            spool = current_app.config['SENT' if demo else 'OUTGOING']
            msg_file = write_outgoing(spool, username(), mobile, data['text'], data.get('queue'), demo)
            message_id = os.path.basename(msg_file)
            result['mobiles'][mobile]['message_id'] = message_id
            result['mobiles'][mobile]['dlr_status'] = os.path.join(os.path.dirname(request.url),
                                                      os.path.basename(current_app.config['SENT']), message_id)
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

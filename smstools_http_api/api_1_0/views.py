#!/usr/bin/env python3

from flask import current_app, jsonify, request

from . import api_1_0
from .authentication import auth
from .errors import bad_request
from .smstools import delete_some_sms, get_some_sms, list_some_sms, send_sms


@api_1_0.route('/monitoring', methods=['GET'])
def monitoring_view():
    return jsonify({'monitoring': 'ok', 'demo': bool(current_app.config.get('DEMO'))})


@api_1_0.route('/sms/<kind>/', methods=['GET'])
@auth.login_required
def list_some_sms_view(kind):
    return list_some_sms(kind)


@api_1_0.route('/sms/<kind>/<message_id>', methods=['GET'])
@auth.login_required
def get_some_sms_view(kind, message_id):
    return get_some_sms(kind, message_id)


@api_1_0.route('/sms/<kind>/<message_id>', methods=['DELETE'])
@auth.login_required
def delete_sms_view(kind, message_id):
    return delete_some_sms(kind, message_id)


@api_1_0.route('/sms/outgoing', methods=['GET', 'POST'])
@auth.login_required
def outgoing_view():
    if request.method == 'POST':
        request_object = request.get_json(silent=True)
    else:
        request_object = {}
        mobiles = request.args.get('mobiles')
        text = request.args.get('text')
        if mobiles:
            # "+" arrives as a space in query strings
            request_object['mobiles'] = mobiles.replace(' ', '+').split(',')
        if text:
            request_object['text'] = text

    if not isinstance(request_object, dict):
        return bad_request('Wrong JSON object')
    for required_field in ('mobiles', 'text'):
        if required_field not in request_object:
            return bad_request('Missing required: {0}'.format(required_field))
    if not isinstance(request_object['mobiles'], list):
        return bad_request('mobiles is not array')
    if len(request_object['mobiles']) == 0:
        return bad_request('mobiles array is empty')
    if not all(isinstance(mobile, str) for mobile in request_object['mobiles']):
        return bad_request('mobiles is not unicode')
    if not isinstance(request_object['text'], str):
        return bad_request('text is not unicode')

    data = {
        'mobiles': request_object['mobiles'],
        'text': request_object['text'],
        'queue': request_object.get('queue') or current_app.config.get('DEFAULTQUEUE'),
    }

    response = jsonify(send_sms(data))
    response.status_code = 201
    return response

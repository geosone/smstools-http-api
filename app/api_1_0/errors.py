#!/usr/bin/env python3

from flask import jsonify, request

from . import api_1_0


def _error(status, message, reason=None):
    body = {'status': status, 'message': '%s: %s' % (message, request.url)}
    if reason:
        body['reason'] = reason
    response = jsonify(body)
    response.status_code = status
    return response


@api_1_0.app_errorhandler(400)
def bad_request(exception):
    return _error(400, 'Bad request', exception if isinstance(exception, str) else None)


def unauthorized(reason):
    return _error(401, 'Unauthorized', reason)


@api_1_0.app_errorhandler(403)
def forbidden(exception):
    return _error(403, 'Forbidden')


@api_1_0.app_errorhandler(404)
def not_found(exception):
    return _error(404, 'Not found')


@api_1_0.app_errorhandler(405)
def not_allowed(exception):
    return _error(405, 'Not allowed')


@api_1_0.app_errorhandler(500)
def internal_error(exception):
    return _error(500, 'Internal error')

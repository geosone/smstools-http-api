#!/usr/bin/env python3

from flask import current_app, g
from flask_httpauth import HTTPBasicAuth
from passlib.apache import HtpasswdFile

from .errors import unauthorized

auth = HTTPBasicAuth()


@auth.verify_password
def verify_password(login, password):
    if not login:
        return None
    try:
        htpasswd_file = HtpasswdFile(current_app.config['HTPASSWD_PATH'])
    except EnvironmentError:
        g.reason = 'no user database'
        return None

    if htpasswd_file.check_password(login, password):
        return login
    g.reason = 'invalid password'
    return None


def username():
    return auth.current_user()


@auth.error_handler
def auth_error(status=401):
    reason = g.get('reason')
    return unauthorized('Unauthorized access: %s' % reason if reason else 'Unauthorized access')

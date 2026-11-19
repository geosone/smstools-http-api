#!/usr/bin/env python3

from flask import Flask

__version__ = '2.1.0'

from .config import config


def create_app(config_name):
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    # Site settings (htpasswd path, whitelist, DEMO, ...) without editing config.py
    app.config.from_envvar('SMSTOOLS_HTTP_API_SETTINGS', silent=True)
    config[config_name].init_app(app)

    from .api_1_0 import api_1_0 as api_1_0_blueprint
    app.register_blueprint(api_1_0_blueprint, url_prefix='/api/v1.0')

    return app

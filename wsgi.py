#!/usr/bin/env python3
"""WSGI entry point: gunicorn wsgi:app (FLASK_CONFIG selects the config, default production)."""

import os

from app import create_app

app = create_app(os.getenv('FLASK_CONFIG') or 'production')

if __name__ == '__main__':
    app.run(host=os.getenv('HOST', '127.0.0.1'), port=int(os.getenv('PORT', '5000')))

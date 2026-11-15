#!/usr/bin/env python3
"""smstools-http-api command: serve the API with gunicorn (or Flask's server with --dev)."""

import argparse
import os
import sys

from . import __version__, create_app


def main(argv=None):
    parser = argparse.ArgumentParser(prog='smstools-http-api', description=__doc__)
    parser.add_argument('-b', '--bind', default=os.getenv('SMSTOOLS_HTTP_API_BIND', '127.0.0.1:5000'),
                        help='address:port to listen on (default %(default)s)')
    parser.add_argument('-w', '--workers', type=int, default=int(os.getenv('SMSTOOLS_HTTP_API_WORKERS', '2')),
                        help='gunicorn worker processes (default %(default)s)')
    parser.add_argument('-c', '--config', default=os.getenv('FLASK_CONFIG', 'production'),
                        choices=['production', 'development'], help='built-in config (default %(default)s)')
    parser.add_argument('--settings', help='settings file (overrides SMSTOOLS_HTTP_API_SETTINGS)')
    parser.add_argument('--dev', action='store_true', help="use Flask's development server")
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    args = parser.parse_args(argv)

    if args.settings:
        os.environ['SMSTOOLS_HTTP_API_SETTINGS'] = args.settings
    app = create_app(args.config)

    if args.dev:
        host, _, port = args.bind.rpartition(':')
        app.run(host=host or '127.0.0.1', port=int(port))
        return 0

    try:
        from gunicorn.app.base import BaseApplication
    except ImportError:
        sys.exit('gunicorn is not installed (pip install gunicorn), or use --dev')

    class Server(BaseApplication):
        def load_config(self):
            self.cfg.set('bind', args.bind)
            self.cfg.set('workers', args.workers)
            self.cfg.set('accesslog', '-')

        def load(self):
            return app

    Server().run()
    return 0


if __name__ == '__main__':
    sys.exit(main())

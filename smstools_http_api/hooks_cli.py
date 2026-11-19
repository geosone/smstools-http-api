#!/usr/bin/env python3
"""Command line entry points for the webhooks.

smstools-http-api-event     smsd eventhandler: queues the event, returns at once
smstools-http-api-webhooks  worker: delivers queued events to the webhooks
"""

import argparse
import os
import signal
import subprocess
import sys
import time

from . import __version__, create_app
from .webhooks import EVENTS, deliver_pending, enqueue, log


def load_config(name, settings):
    if settings:
        os.environ['SMSTOOLS_HTTP_API_SETTINGS'] = settings
    os.environ.setdefault('SMSTOOLS_HTTP_API_SETTINGS', '/etc/smstools-http-api/settings.py')
    # Flask resolves relative paths against the package, not the working directory
    os.environ['SMSTOOLS_HTTP_API_SETTINGS'] = os.path.abspath(os.environ['SMSTOOLS_HTTP_API_SETTINGS'])
    return create_app(name).config


def event_main(argv=None):
    """smsd calls: eventhandler <EVENT> <file> [<message id>]"""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print('usage: smstools-http-api-event EVENT FILE [ID]  (smsd eventhandler)', file=sys.stderr)
        return 2
    event, path, sms_id = argv[0], argv[1], (argv[2] if len(argv) > 2 else None)
    unfiltered = False
    try:
        config = load_config(os.getenv('FLASK_CONFIG', 'production'), None)
    except PermissionError:
        # smsd may run with another group than the settings file (Debian: smsd:dialout).
        # Queue anyway; the worker reads the settings and drops unsubscribed events.
        config = load_config(os.getenv('FLASK_CONFIG', 'production'), os.devnull)
        unfiltered = True
    except Exception as err:  # never make smsd fail because of the settings file
        log('smstools-http-api-event: cannot load settings: %s' % err)
        return 0

    chain = config.get('EVENTHANDLER_CHAIN')
    if chain:
        # an eventhandler that was configured in smsd.conf before
        try:
            subprocess.run([chain] + argv, timeout=config.get('EVENTHANDLER_CHAIN_TIMEOUT', 60))
        except (OSError, subprocess.TimeoutExpired) as err:
            log('smstools-http-api-event: chained handler %s: %s' % (chain, err))

    if event in EVENTS:
        try:
            enqueue(config, event, path, sms_id, unfiltered=unfiltered)
        except OSError as err:
            log('smstools-http-api-event: cannot queue %s %s: %s' % (event, path, err))
    return 0


def worker_main(argv=None):
    parser = argparse.ArgumentParser(prog='smstools-http-api-webhooks',
                                     description='deliver queued smsd events to the webhooks')
    parser.add_argument('-c', '--config', default=os.getenv('FLASK_CONFIG', 'production'),
                        choices=['production', 'development'])
    parser.add_argument('--settings', help='settings file (overrides SMSTOOLS_HTTP_API_SETTINGS)')
    parser.add_argument('--once', action='store_true', help='deliver what is due and exit')
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    args = parser.parse_args(argv)
    config = load_config(args.config, args.settings)

    if args.once:
        counts = deliver_pending(config)
        log('webhooks: %s' % (', '.join('%s %d' % kv for kv in sorted(counts.items())) or 'nothing queued'))
        return 0

    hooks = [h.get('name') for h in config.get('WEBHOOKS') or []]
    log('webhooks: worker started, hooks: %s' % (', '.join(hooks) or 'none'))
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    poll = float(config.get('WEBHOOK_POLL', 2))
    while running:
        deliver_pending(config)
        end = time.monotonic() + poll
        while running and time.monotonic() < end:
            time.sleep(0.2)
    return 0


if __name__ == '__main__':
    # checkout without the console scripts: python3 -m smstools_http_api.hooks_cli event|worker ...
    commands = {'event': event_main, 'worker': worker_main}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit('usage: python3 -m smstools_http_api.hooks_cli {event,worker} ...')
    sys.exit(commands[sys.argv[1]](sys.argv[2:]))

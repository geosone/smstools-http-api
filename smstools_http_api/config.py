#!/usr/bin/env python3

class Config:

    # Valid message kinds (also spooler directories).
    KINDS = ['incoming', 'outgoing', 'checked', 'failed', 'sent']

    # Accounts who may access to all messages
    #ADMIN_ACCOUNTS = ['superadm']

    # Default queue for send message, if not set, keep smsd choose the default queue
    #DEFAULTQUEUE = "GSM1"

    # Limit for list messages of given kind.
    #LIMIT = 10000

    # Demo mode: accept and log messages but do not hand them to smsd (no modem/SIM yet);
    # they are stored in the SENT spool with a "Demo: yes" header.
    DEMO = False

    # Whitelist for users
    #USER_WHITELIST = {
    #    'lvv': [ '911' ],
    #}

    # Webhooks for smsd events, delivered by smstools-http-api-webhooks
    # (see smstools_http_api/webhooks.py and contrib/settings.py.example).
    WEBHOOKS = []
    WEBHOOK_MAX_AGE = 24 * 3600   # give up (move to WEBHOOK_QUEUE/failed) after this
    WEBHOOK_POLL = 2              # worker poll interval, seconds

    # Eventhandler that was configured in smsd.conf before; smstools-http-api-event
    # runs it first with the same arguments.
    #EVENTHANDLER_CHAIN = '/usr/local/bin/my-smsd-handler'

    @staticmethod
    def init_app(app):
        pass

class ProductionConfig(Config):

    # Smsd spool path
    INCOMING = "/var/spool/sms/incoming"
    OUTGOING = "/var/spool/sms/outgoing"
    CHECKED  = "/var/spool/sms/checked"
    FAILED   = "/var/spool/sms/failed"
    SENT     = "/var/spool/sms/sent"
    WEBHOOK_QUEUE = "/var/spool/sms/webhooks"

    # Users database (htpasswd format)
    # Generate hash: openssl passwd -apr1 PASSWORD
    # username:$apr1$qSS22H6v$sem/.bUQXjGUIIHb.MXLw1
    HTPASSWD_PATH = "/etc/smstools-http-api/htpasswd.users"

    @classmethod
    def init_app(cls, app):
        Config.init_app(app)

        import logging

        # stderr -> journald under systemd (replaces Flask's own handler: no double lines)
        from flask.logging import default_handler
        app.logger.removeHandler(default_handler)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        app.logger.setLevel(logging.INFO)
        app.logger.addHandler(handler)

class DevelopmentConfig(Config):
    DEBUG = True

    # Smsd spool path
    INCOMING = "tmp/incoming"
    OUTGOING = "tmp/outgoing"
    CHECKED  = "tmp/checked"
    FAILED   = "tmp/failed"
    SENT     = "tmp/sent"
    WEBHOOK_QUEUE = "tmp/webhooks"

    # Users database (htpasswd format)
    # Generate hash: openssl passwd -apr1 PASSWORD
    # username:$apr1$qSS22H6v$sem/.bUQXjGUIIHb.MXLw1
    HTPASSWD_PATH="htpasswd.users"

class TestConfig(Config):
    TESTING = True
    DEBUG = True
    INCOMING = "tmp/test/incoming"
    OUTGOING = "tmp/test/outgoing"
    CHECKED = "tmp/test/checked"
    FAILED = "tmp/test/failed"
    SENT = "tmp/test/sent"
    WEBHOOK_QUEUE = "tmp/test/webhooks"
    HTPASSWD_PATH = "tmp/test/htpasswd.users"

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'test': TestConfig,
    'default': DevelopmentConfig
}

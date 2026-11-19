#!/usr/bin/env python3
"""Webhooks for smsd events (received, sent, failed, ...).

smsd calls the event handler (smstools-http-api-event) for every message.  The
handler only writes the event as a JSON file into WEBHOOK_QUEUE, so it never
blocks the modem handler.  The webhook worker (smstools-http-api-webhooks)
delivers the queued events to every subscribed webhook and retries failed
deliveries with a backoff until WEBHOOK_MAX_AGE; then the event is moved to
WEBHOOK_QUEUE/failed.

A webhook (an entry of WEBHOOKS in the settings file) is a dict:

    name      unique name, used to remember which hooks already got an event
    url       http(s) URL; the event is POSTed as JSON
    events    smsd events to deliver (default ['RECEIVED'])
    headers   extra request headers, e.g. {'Authorization': 'Bearer ...'}
    auth      (user, password) for HTTP basic auth
    timeout   seconds per request (default 10)
    verify    False skips TLS certificate verification (default: verify)
    filter    callable(event) -> bool: deliver only matching events
    payload   callable(event) -> JSON-serialisable body (default: the event)
    check     callable(status, body) -> bool: success test (default: 2xx)

A hook with 'type': 'email' mails the event instead of POSTing it:

    to          address or list of addresses
    from        sender (default smstools-http-api@<fqdn>)
    smtp_host   (default localhost), smtp_port (25, 465 with smtp_ssl)
    smtp_ssl    SMTP over TLS; starttls: STARTTLS; smtp_auth: (user, password)
    subject     string or callable(event) (default "SMS from <number>")
    body        string or callable(event) (default: text plus the main headers)

A hook with 'type': 'route' mails a received SMS to the contacts (CONTACTS, see
contacts.py) named by its @tokens, anywhere in the text ("@mum @rene see you at 6",
"hi @kids dinner is ready"):

    domain       mail2sms domain; the mail comes from <number>@domain, so replies are SMS
    default_to   address(es) for SMS without (known) tokens
    skip         tokens other hooks handle (e.g. ['zabbix'], with a filter on parse_tokens)
    mail_domains prefer a contact's address in these domains
    texts        {'subject': 'SMS from {name}', 'footer': ..., 'unknown': ...{names}}
    plus the SMTP settings of type 'email'.
"""

import base64
import datetime
import email.message
import email.utils
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request

from . import contacts
from .message import normalize_number, read_message

EVENTS = ('RECEIVED', 'SENT', 'FAILED', 'REPORT', 'CALL')
DEFAULT_QUEUE = '/var/spool/sms/webhooks'
DEFAULT_MAX_AGE = 24 * 3600
MAX_BACKOFF = 3600


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def queue_dir(config):
    return config.get('WEBHOOK_QUEUE') or DEFAULT_QUEUE


def hooks_for(config, event):
    return [h for h in config.get('WEBHOOKS') or [] if event in h.get('events', ['RECEIVED'])]


def build_event(event, path, sms_id=None):
    """Turn an smsd event (handler arguments) into the JSON event that is delivered."""
    try:
        message = read_message(path)
    except OSError:
        message = {'message_id': os.path.basename(path)}
    text = message.pop('text', '')
    message_id = message.pop('message_id')
    return {
        'event': event,
        'message_id': message_id,
        'kind': os.path.basename(os.path.dirname(os.path.abspath(path))),
        'from': message.get('From'),
        'to': message.get('To'),
        'modem': message.get('Modem'),
        'sms_id': sms_id,
        'text': text,
        'headers': message,
        'time': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
    }


def enqueue(config, event, path, sms_id=None, unfiltered=False):
    """Queue an smsd event for the subscribed webhooks; returns the queue file or None.

    unfiltered: queue even without a subscribed hook (the eventhandler could not read
    the settings); the worker drops events nobody subscribed to.
    """
    if not unfiltered and not hooks_for(config, event):
        return None
    record = {'event': build_event(event, path, sms_id), 'delivered': [], 'attempts': 0,
              'next_try': 0, 'created': time.time()}
    qdir = queue_dir(config)
    os.makedirs(qdir, exist_ok=True)
    # name sorts by time, so the worker delivers in arrival order
    fd, tmp = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=qdir)
    with os.fdopen(fd, 'w') as fp:
        json.dump(record, fp)
    final = os.path.join(qdir, '%.6f-%s.json' % (time.time(), record['event']['message_id']))
    os.rename(tmp, final)
    return final


def default_subject(event):
    who = event.get('from') if event['event'] == 'RECEIVED' else event.get('to')
    return 'SMS %s %s' % (EVENT_WORDS.get(event['event'], event['event']), who or '')


def default_body(event):
    lines = [event.get('text') or '', '', '--']
    for key in ('From', 'To', 'Sent', 'Received', 'Modem', 'Fail_reason'):
        if event['headers'].get(key):
            lines.append('%s: %s' % (key, event['headers'][key]))
    lines.append('Event: %s %s' % (event['event'], event['message_id']))
    return '\n'.join(lines) + '\n'


EVENT_WORDS = {'RECEIVED': 'from', 'SENT': 'sent to', 'FAILED': 'failed to', 'REPORT': 'report for'}


def _as_list(value):
    return list(value) if isinstance(value, (list, tuple)) else ([value] if value else [])


def send_mail(hook, event):
    """Mail one event (hook type 'email'); returns (ok, detail)."""
    msg = email.message.EmailMessage()
    msg['From'] = hook.get('from', 'smstools-http-api@%s' % socket.getfqdn())
    msg['To'] = ', '.join(_as_list(hook['to']))
    subject = hook.get('subject', default_subject)
    msg['Subject'] = subject(event) if callable(subject) else subject
    body = hook.get('body', default_body)
    msg.set_content(body(event) if callable(body) else body)
    return smtp_send(hook, msg)


_TOKEN = re.compile(r'(?<![\w.+-])@([\w][\w.+-]*)')


def find_tokens(text):
    """All '@name' tokens of a text, anywhere (not mail addresses): lower case, in order."""
    tokens = []
    for token in _TOKEN.findall(text or ''):
        token = token.lower().rstrip('.-')
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def parse_tokens(text):
    """'@mum @rene hi @x' -> (['mum', 'rene', 'x'], 'hi @x'): every token, and the text
    without the tokens at its start (tokens inside the text stay, they are part of it)."""
    text = text or ''
    match = re.match(r'\s*((?:@[\w.+-]+[\s,;:]*)+)', text)
    return find_tokens(text), (text[match.end():] if match else text).strip()


ROUTE_DEFAULTS = {
    'subject': 'SMS from {name}',
    'footer': 'Reply to this mail to answer by SMS.',
    'unknown': 'Unknown recipients: {names}',
}


def route_mail(hook, event, config):
    """Hook type 'route': mail a received SMS to the contacts named by its leading @tokens.

    Tokens in 'skip' are left to other hooks (e.g. @zabbix); an SMS without (known)
    tokens goes to 'default_to'.  The mail comes from <number>@<domain>, so a reply
    goes back through mail2sms.
    """
    tokens, text = parse_tokens(event.get('text'))
    book = contacts.directory(config, log=log)
    skip = {s.lower() for s in hook.get('skip', [])}
    domains = [d.lower() for d in hook.get('mail_domains', [])]
    to, unknown = [], []
    for token in tokens:
        if token in skip:
            continue
        cards = [c for c in book.resolve(token) if c['emails']]
        if not cards:
            unknown.append(token)
        for card in cards:
            preferred = [e for e in card['emails'] if e.rpartition('@')[2].lower() in domains]
            address = (preferred or card['emails'])[0]
            if address.lower() not in (a.lower() for a in to):
                to.append(address)
    if not to and (unknown or not set(tokens) & skip):
        to = _as_list(hook['default_to'])
    if not to:
        return True, 'left to other hooks (%s)' % ', '.join('@' + t for t in tokens)

    number = normalize_number(event.get('from') or '', config.get('COUNTRY_CODE', '43'))
    known = book.by_number(number) if number.isdigit() else []
    name = known[0]['name'] if known else ('+' + number if number.isdigit() else event.get('from') or '?')
    words = dict(ROUTE_DEFAULTS, **hook.get('texts', {}))
    msg = email.message.EmailMessage()
    if number.isdigit():
        reply = email.utils.formataddr(('%s (SMS)' % name, '%s@%s' % (number, hook['domain'])))
        msg['From'] = reply
    else:  # alphanumeric sender, cannot be answered
        msg['From'] = hook.get('from', 'smstools-http-api@%s' % socket.getfqdn())
    msg['To'] = ', '.join(to)
    msg['Subject'] = words['subject'].format(name=name, number=number)
    lines = [text, '', '--']
    if unknown:
        lines.append(words['unknown'].format(names=', '.join('@' + t for t in unknown)))
    if number.isdigit():
        lines.append(words['footer'].format(name=name, number=number))
    lines.append('%s: %s' % (event['message_id'], event['headers'].get('Received') or event['headers'].get('Sent', '')))
    msg.set_content('\n'.join(lines) + '\n')
    return smtp_send(hook, msg)


def smtp_send(hook, msg):
    """Send a mail with the SMTP settings of a hook; returns (ok, detail)."""
    if 'Date' not in msg:
        msg['Date'] = email.utils.formatdate(localtime=True)
    if 'Message-ID' not in msg:
        msg['Message-ID'] = email.utils.make_msgid(
            domain=email.utils.parseaddr(msg['From'])[1].rpartition('@')[2] or None)
    try:
        smtp_class = smtplib.SMTP_SSL if hook.get('smtp_ssl') else smtplib.SMTP
        with smtp_class(hook.get('smtp_host', 'localhost'), hook.get('smtp_port', 465 if hook.get('smtp_ssl') else 25),
                        timeout=hook.get('timeout', 30)) as smtp:
            if hook.get('starttls'):
                smtp.starttls(context=ssl.create_default_context())
            if hook.get('smtp_auth'):
                smtp.login(*hook['smtp_auth'])
            refused = smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as err:
        return False, 'SMTP %s' % err
    if refused:
        return False, 'SMTP refused %s' % ', '.join(refused)
    return True, 'mailed to %s' % msg['To']


def post(hook, event, config=None):
    """Deliver one event to one hook (HTTP POST, mail for type 'email', routed mail for
    type 'route'); returns (ok, detail)."""
    if hook.get('type') == 'email':
        return send_mail(hook, event)
    if hook.get('type') == 'route':
        try:
            return route_mail(hook, event, config or {})
        except Exception as err:  # e.g. a broken address book must not kill the worker
            return False, 'route failed: %s' % err
    body = hook['payload'](event) if callable(hook.get('payload')) else event
    data = json.dumps(body).encode()
    request = urllib.request.Request(hook['url'], data=data, method='POST')
    request.add_header('Content-Type', 'application/json; charset=utf-8')
    request.add_header('User-Agent', 'smstools-http-api')
    for key, val in (hook.get('headers') or {}).items():
        request.add_header(key, val)
    if hook.get('auth'):
        token = base64.b64encode(('%s:%s' % tuple(hook['auth'])).encode()).decode()
        request.add_header('Authorization', 'Basic ' + token)
    context = None
    if hook.get('verify') is False:
        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=hook.get('timeout', 10), context=context) as resp:
            status, text = resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as err:
        status, text = err.code, err.read().decode('utf-8', errors='replace')
    except (urllib.error.URLError, OSError) as err:
        return False, str(getattr(err, 'reason', err))
    if callable(hook.get('check')):
        try:
            ok = bool(hook['check'](status, text))
        except Exception as err:  # a broken check must not kill the worker
            return False, 'check failed: %s' % err
    else:
        ok = 200 <= status < 300
    return ok, 'HTTP %s %s' % (status, text[:200].replace('\n', ' '))


def deliver_file(config, path, now=None):
    """Deliver one queued event; returns 'done', 'retry', 'failed' or 'wait'."""
    now = time.time() if now is None else now
    with open(path) as fp:
        record = json.load(fp)
    if record.get('next_try', 0) > now:
        return 'wait'

    event = record['event']
    pending = [h for h in hooks_for(config, event['event']) if h['name'] not in record['delivered']]
    for hook in pending:
        if callable(hook.get('filter')):
            try:
                wanted = hook['filter'](event)
            except Exception as err:
                log('webhook %s: filter failed for %s: %s' % (hook['name'], event['message_id'], err))
                wanted = False
            if not wanted:
                record['delivered'].append(hook['name'])
                continue
        ok, detail = post(hook, event, config)
        if ok:
            record['delivered'].append(hook['name'])
            log('webhook %s: %s %s delivered (%s)' % (hook['name'], event['event'], event['message_id'], detail))
        else:
            log('webhook %s: %s %s failed: %s' % (hook['name'], event['event'], event['message_id'], detail))

    if all(h['name'] in record['delivered'] for h in hooks_for(config, event['event'])):
        os.remove(path)
        return 'done'

    record['attempts'] += 1
    if now - record.get('created', now) > config.get('WEBHOOK_MAX_AGE', DEFAULT_MAX_AGE):
        failed = os.path.join(os.path.dirname(path), 'failed')
        os.makedirs(failed, exist_ok=True)
        os.rename(path, os.path.join(failed, os.path.basename(path)))
        log('webhook: giving up on %s %s after %d attempts' % (event['event'], event['message_id'], record['attempts']))
        return 'failed'
    record['next_try'] = now + min(30 * 2 ** (record['attempts'] - 1), MAX_BACKOFF)
    tmp = path + '.tmp'
    with open(tmp, 'w') as fp:
        json.dump(record, fp)
    os.rename(tmp, path)
    return 'retry'


def deliver_pending(config, now=None):
    """Deliver everything that is due; returns a dict of result counts."""
    qdir = queue_dir(config)
    counts = {}
    try:
        names = sorted(n for n in os.listdir(qdir) if n.endswith('.json'))
    except FileNotFoundError:
        return counts
    for name in names:
        try:
            result = deliver_file(config, os.path.join(qdir, name), now)
        except (OSError, ValueError, KeyError) as err:
            log('webhook: cannot process %s: %s' % (name, err))
            result = 'error'
        counts[result] = counts.get(result, 0) + 1
    return counts

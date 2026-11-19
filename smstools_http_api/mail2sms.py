#!/usr/bin/env python3
"""mail2sms: turn a mail to <name>@<sms domain> into SMS.

Run by the MTA for every recipient, e.g. a Postfix pipe transport (one recipient
per call, smstools-http-api-mail2sms_destination_recipient_limit = 1):

    smstools-http-api-mail2sms unix - n n - 4 pipe
      flags=Rq user=smsd argv=/usr/bin/smstools-http-api-mail2sms ${sender} ${recipient}

The local part of the recipient is a number (436761234567@, +436761234567@) or a
name of the address book (CONTACTS): a nickname, a group (category), a given or full
name.  A group sends to all its members except the sender.

Only senders in MAIL2SMS_SENDERS or with an address of a contact (restricted to
MAIL2SMS_SENDER_DOMAINS if set) may send.  With MAIL2SMS_TRUSTED_CLIENTS (hosts or
networks, e.g. the own mail server with authenticated submission), mail from any other
client (--client ${client_address}) needs a "Received-SPF: pass" header, as the own
mail gateway adds it, so a forged sender address is refused.  MAIL2SMS_SPF_RECEIVER (the
gateway's host name) only accepts the gateway's own header (receiver=<gateway>, above
the hop where the mail entered the gateway), not one the sender wrote into the mail.  The SMS text is the text part of the mail
without quoted lines and signature, or the subject if that is empty, prefixed with the
sender's given name (MAIL2SMS_PREFIX) and cut to MAIL2SMS_MAX_PARTS SMS.

Exit codes follow sysexits.h, so the MTA bounces with a reason: 67 unknown recipient,
77 sender not allowed, 65 no text, 75 temporary error (MTA retries).
"""

import argparse
import email
import email.policy
import html
import ipaddress
import os
import re
import sys

from . import __version__, contacts
from .hooks_cli import load_config
from .message import normalize_number, truncate_text, write_outgoing
from .webhooks import log

EX_DATAERR, EX_NOUSER, EX_TEMPFAIL, EX_NOPERM = 65, 67, 75, 77

_QUOTE_HEADER = re.compile(r'^\s*(Am |On |Le |El ).*(schrieb|wrote|a écrit|escribió).*:\s*$', re.I)
_ORIGINAL = re.compile(r'^\s*(-{2,}\s*(Original|Ursprüngliche)|_{5,}|(Von|From):\s)', re.I)


def mail_text(msg):
    """Plain text of a mail without quotes and signature (subject if empty)."""
    part = msg.get_body(preferencelist=('plain', 'html'))
    text = ''
    if part is not None:
        text = part.get_content()
        if part.get_content_type() == 'text/html':
            text = re.sub(r'(?is)<(script|style).*?</\1>', '', text)
            text = re.sub(r'(?i)<br\s*/?>|</p>|</div>', '\n', text)
            text = html.unescape(re.sub(r'<[^>]+>', '', text))
    lines = []
    for line in text.splitlines():
        if line.rstrip() == '--' or _QUOTE_HEADER.match(line) or _ORIGINAL.match(line):
            break
        if line.lstrip().startswith('>'):
            continue
        lines.append(line.rstrip())
    text = re.sub(r'\n{2,}', '\n', '\n'.join(lines)).strip()
    text = re.sub(r'[ \t]+', ' ', text)
    return text or str(msg.get('Subject', '')).strip()


def trusted_client(config, client):
    """True if the mail came from a MAIL2SMS_TRUSTED_CLIENTS host (or no list is set)."""
    trusted = config.get('MAIL2SMS_TRUSTED_CLIENTS')
    if not trusted:
        return True
    try:
        address = ipaddress.ip_address((client or '').strip('[]'))
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(net, strict=False) for net in trusted)


def spf_pass(msg, receiver=None):
    """The own mail gateway checked SPF and it passed.

    Without receiver: the topmost Received-SPF header says pass.  With receiver (the
    gateway's host name, MAIL2SMS_SPF_RECEIVER): a 'pass' with receiver=<gateway> is only
    counted above the hop where the mail entered the gateway ("Received: from <other
    host> by <gateway>"); headers below it come from the sender and may be forged.
    """
    gateway = (receiver or '').lower()
    for name, value in msg.items():   # top-down: newest header first
        name, value = name.lower(), ' '.join(str(value).split())
        if name == 'received-spf':
            if not gateway:
                return value.lower().startswith('pass')
            if value.lower().startswith('pass') and \
                    re.search(r'\breceiver=%s(?![\w.-])' % re.escape(gateway), value, re.I):
                return True
        elif name == 'received' and gateway:
            hop = re.match(r'from\s+(\S+).*?\bby\s+(\S+)', value, re.I)
            if hop and hop.group(2).lower() == gateway and hop.group(1).lower() not in (gateway, 'localhost'):
                return False   # entry into the gateway: everything below is the sender's
    return False


def allowed_sender(config, book, sender):
    sender = (sender or '').strip().lower()
    if sender in (s.lower() for s in config.get('MAIL2SMS_SENDERS', [])):
        return True
    domains = [d.lower() for d in config.get('MAIL2SMS_SENDER_DOMAINS', [])]
    if domains and sender.rpartition('@')[2] not in domains:
        return False
    return bool(book.by_email(sender))


def recipients(config, book, local, sender_cards):
    """Numbers a recipient local part stands for (empty if unknown)."""
    cc = config.get('COUNTRY_CODE', '43')
    if re.fullmatch(r'\+?\d{5,}', local):
        return [normalize_number(local if local.startswith('+') else '+' + local, cc)]
    own = {n for card in sender_cards for n in book.numbers(card)}
    cards = book.resolve(local)
    numbers = []
    for card in cards:
        for number in book.numbers(card)[:1]:
            if number not in numbers and (number not in own or len(cards) == 1):
                numbers.append(number)
    return numbers


def main(argv=None):
    parser = argparse.ArgumentParser(prog='smstools-http-api-mail2sms',
                                     description='send a mail (stdin) as SMS; for the MTA')
    parser.add_argument('-c', '--config', default=os.getenv('FLASK_CONFIG', 'production'),
                        choices=['production', 'development', 'test'])
    parser.add_argument('--settings', help='settings file (overrides SMSTOOLS_HTTP_API_SETTINGS)')
    parser.add_argument('--dry-run', action='store_true', help='print what would be sent')
    parser.add_argument('--client', help='address of the SMTP client (Postfix ${client_address})')
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    parser.add_argument('sender')
    parser.add_argument('recipient')
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config, args.settings)
        book = contacts.directory(config, log=log)
    except Exception as err:
        print('mail2sms: cannot load settings/contacts: %s' % err)
        return EX_TEMPFAIL

    if not allowed_sender(config, book, args.sender):
        print('mail2sms: sender %s may not send SMS' % args.sender)
        log('mail2sms: refused sender %s -> %s' % (args.sender, args.recipient))
        return EX_NOPERM

    local = args.recipient.rpartition('@')[0].split('+')[0] if '@' in args.recipient else args.recipient
    sender_cards = book.by_email(args.sender)
    numbers = recipients(config, book, local.lower(), sender_cards)
    if not numbers:
        print('mail2sms: unknown SMS recipient %s' % local)
        log('mail2sms: unknown recipient %s from %s' % (args.recipient, args.sender))
        return EX_NOUSER

    msg = email.message_from_binary_file(sys.stdin.buffer, policy=email.policy.default)
    if not trusted_client(config, args.client) and not spf_pass(msg, config.get('MAIL2SMS_SPF_RECEIVER')):
        print('mail2sms: sender %s not verified (SPF)' % args.sender)
        log('mail2sms: refused unverified sender %s (client %s) -> %s'
            % (args.sender, args.client, args.recipient))
        return EX_NOPERM
    text = mail_text(msg)
    if not text:
        print('mail2sms: the mail has no text')
        return EX_DATAERR
    if config.get('MAIL2SMS_PREFIX', True) and sender_cards and sender_cards[0]['given']:
        text = '%s: %s' % (sender_cards[0]['given'], text)
    text = truncate_text(text, int(config.get('MAIL2SMS_MAX_PARTS', 3)))

    demo = bool(config.get('DEMO'))
    spool = config['SENT' if demo else 'OUTGOING']
    for number in numbers:
        if args.dry_run:
            print('%s -> %s: %s' % (args.sender, number, text))
            continue
        try:
            path = write_outgoing(spool, 'mail:%s' % args.sender, number, text,
                                  config.get('DEFAULTQUEUE'), demo)
        except OSError as err:
            print('mail2sms: cannot spool the SMS: %s' % err)
            return EX_TEMPFAIL
        log('mail2sms: %s -> %s (%s) spooled as %s%s'
            % (args.sender, number, local, os.path.basename(path), ' (DEMO)' if demo else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())

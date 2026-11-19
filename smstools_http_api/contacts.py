#!/usr/bin/env python3
"""Address book for SMS routing and mail2sms (vCards from CardDAV or a file).

CONTACTS in the settings file:

    {'url': 'https://cloud.example.org/remote.php/dav/addressbooks/users/smsgw/family/?export',
     'auth': ('smsgw', 'app-password'),  # HTTP basic auth (optional)
     'ttl': 900,                         # seconds before the address book is fetched again
     'cache': '/var/spool/sms/webhooks/contacts.vcf'}   # default: in WEBHOOK_QUEUE
or
    {'file': '/etc/smstools-http-api/contacts.vcf'}

The url is a CardDAV address book collection; '?export' (Nextcloud, SabreDAV) returns
all its vCards in one response. When fetching fails, the cached copy is used.

A contact answers to (case-insensitive): its NICKNAMEs, CATEGORIES (groups), given
name, full name (also without spaces or with dots), the local parts of its e-mail
addresses and its phone numbers.
"""

import base64
import os
import re
import tempfile
import time
import urllib.request

from .message import normalize_number


def _unfold(text):
    return re.sub(r'\r?\n[ \t]', '', text).splitlines()


def _unescape(value):
    return re.sub(r'\\(.)', lambda m: '\n' if m.group(1) in 'nN' else m.group(1), value)


def _split(value, sep):
    """Split a vCard value at unescaped separators."""
    return [_unescape(p).strip() for p in re.split(r'(?<!\\)' + re.escape(sep), value)]


def parse_vcards(text):
    """Return a list of contacts: dicts with name, given, nicknames, categories, emails, phones, cells."""
    contacts, card = [], None
    for line in _unfold(text):
        if not line.strip():
            continue
        name_part, _, value = line.partition(':')
        prop, *params = name_part.split(';')
        prop = prop.split('.')[-1].upper()   # drop group prefix (item1.EMAIL)
        params = ';'.join(params).upper()
        if prop == 'BEGIN' and value.upper() == 'VCARD':
            card = {'name': '', 'given': '', 'nicknames': [], 'categories': [],
                    'emails': [], 'phones': [], 'cells': []}
        elif card is None:
            continue
        elif prop == 'END':
            if card['name'] or card['given']:
                contacts.append(card)
            card = None
        elif prop == 'FN':
            card['name'] = _unescape(value).strip()
        elif prop == 'N':
            parts = _split(value, ';') + ['', '']
            card['given'] = card['given'] or parts[1]
        elif prop == 'NICKNAME':
            card['nicknames'] += [v for v in _split(value, ',') if v]
        elif prop == 'CATEGORIES':
            card['categories'] += [v for v in _split(value, ',') if v]
        elif prop == 'EMAIL':
            addr = _unescape(value).strip()
            if addr:
                # TYPE=PREF / PREF=1 first
                (card['emails'].insert(0, addr) if 'PREF' in params else card['emails'].append(addr))
        elif prop == 'TEL':
            number = _unescape(value).strip()
            if number.lower().startswith('tel:'):
                number = number[4:]
            if number:
                card['phones'].append(number)
                if 'CELL' in params:
                    card['cells'].append(number)
    for card in contacts:
        if not card['name']:
            card['name'] = card['given']
        if not card['given']:
            card['given'] = card['name'].split()[0] if card['name'] else ''
    return contacts


class Directory:
    """Lookup of contacts by short name, group, e-mail address or number."""

    def __init__(self, contacts, country_code='43'):
        self.contacts = contacts
        self.country_code = country_code
        self._names = {}
        for card in contacts:
            keys = set(card['nicknames']) | set(card['categories'])
            if card['given']:
                keys.add(card['given'])
            if card['name']:
                keys |= {card['name'], card['name'].replace(' ', ''), card['name'].replace(' ', '.')}
            keys |= {e.partition('@')[0] for e in card['emails']}
            for key in keys:
                self._names.setdefault(key.lower(), []).append(card)

    def numbers(self, card):
        """Mobile numbers of a contact (CELL, else any phone), normalized."""
        return [normalize_number(n, self.country_code) for n in (card['cells'] or card['phones'])]

    def resolve(self, name):
        """Contacts a short name/group/full name/number stands for (may be empty)."""
        name = (name or '').strip().lower()
        if re.fullmatch(r'\+?\d{5,}', name):
            return self.by_number(name) or [{'name': '', 'given': '', 'nicknames': [], 'categories': [],
                                             'emails': [], 'phones': [name], 'cells': [name]}]
        return list(self._names.get(name, []))

    def by_email(self, address):
        address = (address or '').strip().lower()
        return [c for c in self.contacts if address in (e.lower() for e in c['emails'])]

    def by_number(self, number):
        number = normalize_number(number, self.country_code)
        return [c for c in self.contacts if number in self.numbers(c)] if number else []


def _fetch(source):
    request = urllib.request.Request(source['url'], headers={'User-Agent': 'smstools-http-api'})
    if source.get('auth'):
        token = base64.b64encode(('%s:%s' % tuple(source['auth'])).encode()).decode()
        request.add_header('Authorization', 'Basic ' + token)
    with urllib.request.urlopen(request, timeout=source.get('timeout', 20)) as resp:
        return resp.read().decode('utf-8', errors='replace')


def load_vcards(config, now=None, log=None):
    """vCard text of the CONTACTS source ('' if none), fetched at most every ttl seconds."""
    source = config.get('CONTACTS') or {}
    if source.get('file'):
        with open(source['file'], encoding='utf-8', errors='replace') as fp:
            return fp.read()
    if not source.get('url'):
        return ''
    now = time.time() if now is None else now
    cache = source.get('cache') or os.path.join(config.get('WEBHOOK_QUEUE') or '/var/spool/sms/webhooks',
                                                'contacts.vcf')
    try:
        fresh = now - os.stat(cache).st_mtime < source.get('ttl', 900)
    except OSError:
        fresh = False
    if not fresh:
        try:
            text = _fetch(source)
            if 'BEGIN:VCARD' not in text.upper() and text.strip():
                raise ValueError('no vCards in the response')
            fd, tmp = tempfile.mkstemp(prefix='.contacts', dir=os.path.dirname(cache))
            with os.fdopen(fd, 'w', encoding='utf-8') as fp:
                fp.write(text)
            os.chmod(tmp, 0o640)
            os.rename(tmp, cache)
            return text
        except Exception as err:  # use the cached copy
            if log:
                log('contacts: cannot fetch %s: %s' % (source['url'].split('?')[0], err))
    try:
        with open(cache, encoding='utf-8', errors='replace') as fp:
            return fp.read()
    except OSError:
        return ''


def directory(config, now=None, log=None):
    return Directory(parse_vcards(load_vcards(config, now, log)), config.get('COUNTRY_CODE', '43'))

#!/usr/bin/env python3
"""Read and write smsd spool files (shared by the API, the webhooks and mail2sms)."""

import os
import re
import uuid
from email.message import Message

GSM_PART, UCS_PART = 153, 67


def encode_text(text):
    """Return (bytes, smsd alphabet, number of SMS parts) for a message text."""
    try:
        data, coding, per_part = text.encode('ascii'), 'ISO', GSM_PART
    except UnicodeEncodeError:
        data, coding, per_part = text.encode('utf-16-be'), 'UCS2', UCS_PART
    return data, coding, len(text) // per_part + (len(text) % per_part > 0)


def truncate_text(text, max_parts):
    """Shorten text to fit into max_parts SMS (ending with an ellipsis if cut)."""
    per_part = GSM_PART if text.isascii() else UCS_PART
    limit = per_part * max_parts
    if len(text) <= limit:
        return text
    cut = limit - (3 if text.isascii() else 1)
    return text[:cut].rstrip() + ('...' if text.isascii() else '…')


def normalize_number(number, country_code='43'):
    """'+43 676 ...', '0043676...', '0676...' -> '43676...' (digits, as smsd wants them)."""
    number = (number or '').strip()
    digits = re.sub(r'\D', '', number)
    if number.startswith('+'):
        return digits
    if digits.startswith('00'):
        return digits[2:]
    if digits.startswith('0') and country_code:
        return country_code + digits[1:]
    return digits


def write_outgoing(spool, sender, mobile, text, queue=None, demo=False):
    """Hand one SMS to smsd (spool file, written under .LOCK and renamed); returns the path.

    demo: the spool is the "sent" directory and the message is marked "Demo: yes".
    """
    data, coding, _ = encode_text(text)
    m = Message()
    m.add_header('From', sender)
    m.add_header('To', mobile)
    m.add_header('Alphabet', coding)
    if demo:
        m.add_header('Demo', 'yes')
    if queue:
        m.add_header('Queue', queue)
    m.set_payload(data)
    path = os.path.join(spool, str(uuid.uuid4()))
    with open(path + '.LOCK', 'wb') as fp:
        fp.write(m.as_bytes())
    os.rename(path + '.LOCK', path)
    os.chmod(path, 0o660)
    return path


def read_message(path):
    """Return the headers of an smsd message file as a dict plus 'text' and 'message_id'.

    Raises OSError if the file cannot be read.
    """
    result = {}
    with open(path, 'rb') as fp:
        for raw in fp:
            line = raw.decode('utf-8', errors='replace')
            if line.strip() == '':
                break
            try:
                key, val = line.split(':', 1)
                result[key] = val.strip()
            except ValueError:
                pass
        body = fp.read()

    if result.get('Alphabet', '').startswith('UCS'):
        charset = 'utf-16-be'
    else:
        # UTF-8 is backwards compatible with US-ASCII, and messages written from
        # the command line are UTF-8 without an Alphabet header.
        charset = 'utf-8'
    if body:
        result['text'] = body.decode(charset, errors='replace')
    result['message_id'] = os.path.basename(path)
    return result

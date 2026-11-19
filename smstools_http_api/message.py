#!/usr/bin/env python3
"""Read smsd spool files (shared by the API and the webhooks)."""

import os


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

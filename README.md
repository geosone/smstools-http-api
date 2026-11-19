smstools-http-api
=================

REST HTTP API with Flask for SMS Server Tools 3 (http://smstools3.kekekasvi.com/)


Fork of [lvasiliev/smstools-http-api](https://github.com/lvasiliev/smstools-http-api),
updated for Python 3 and current Flask (2.2+/3.x), Flask-HTTPAuth 4 and gunicorn.

Changes against upstream:

- Python 3 only, packaged as `smstools_http_api` with a `smstools-http-api` command;
  `flask_script` (unmaintained) dropped, `wsgi.py` for other WSGI servers
- Debian/Ubuntu packages, wheel and container image built by GitHub Actions
- runs with the Debian packages (trixie: `python3-flask python3-flask-httpauth python3-passlib gunicorn`)
- site settings in a separate file (`SMSTOOLS_HTTP_API_SETTINGS`), see `contrib/settings.py.example`
- `DEMO` mode: messages are accepted, logged and stored in the `sent` spool with `Demo: yes`
  instead of being handed to smsd (for setups without modem/SIM yet)
- webhooks: received (and sent/failed/...) messages are POSTed to configured URLs or mailed,
  with retries and a custom body per hook (e.g. Zabbix `history.push`), see [Webhooks](#webhooks)
- address book from CardDAV (e.g. Nextcloud) for SMS routing: `@mum @dad ...` at the
  start of a received SMS mails it to those contacts, and mails to `mum@sms.example.org`
  or `436761234567@sms.example.org` are sent as SMS, see [Routing and mail2sms](#routing-and-mail2sms)
- fixed: listing messages recursed endlessly, message IDs could escape the spool directory
  (`../`), JSON errors keys (`"status: "` -> `"status"`), headers with `:` in the value,
  wrong Content-Type gave 415 instead of 400; `POST/GET /sms/outgoing` answers 201 as documented

Installation
------------

The API reads and writes the spool directories of smsd, so it runs on the host (or in a
container next to the one) where SMS Server Tools 3 runs.

### Debian / Ubuntu package

Packages for Debian 12/13 and Ubuntu 24.04/26.04 are attached to the
[releases](https://github.com/geosone/smstools-http-api/releases) (builds of every
commit are in the [Actions](https://github.com/geosone/smstools-http-api/actions) artifacts):

    # apt install ./smstools-http-api_*.deb
    # htpasswd -B /etc/smstools-http-api/htpasswd.users myuser
    # editor /etc/smstools-http-api/settings.py /etc/default/smstools-http-api
    # systemctl restart smstools-http-api

It depends on `smstools` and runs as `smsd`, listening on `127.0.0.1:5000`
(`SMSTOOLS_HTTP_API_BIND` in `/etc/default/smstools-http-api`).

### pip / pipx

    $ pipx install 'smstools-http-api[server] @ git+https://github.com/geosone/smstools-http-api'
    $ smstools-http-api --bind 0.0.0.0:5000 --settings /etc/smstools-http-api/settings.py

or the wheel from a release: `pip install 'smstools_http_api-*.whl[server]'`.
`contrib/smstools-http-api.service` is a systemd unit for a checkout in
`/opt/smstools-http-api` served by gunicorn (`wsgi:app`).

### Container

    # docker run -d -p 5000:5000 --user "$(id -u smsd):$(id -g smsd)" \
        -v /var/spool/sms:/var/spool/sms \
        -v /etc/smstools-http-api:/etc/smstools-http-api:ro \
        ghcr.io/geosone/smstools-http-api

### Configuration

Settings go to `/etc/smstools-http-api/settings.py` (see `contrib/settings.py.example`):
`DEMO`, `HTPASSWD_PATH`, `ADMIN_ACCOUNTS`, `DEFAULTQUEUE`, `USER_WHITELIST`. Users are
in an htpasswd file (`htpasswd -B`). Basic auth travels in clear text: listen on a
trusted network only, or behind a TLS reverse proxy.

### Webhooks

smsd runs an *eventhandler* for every message. `smstools-http-api-event` only writes
the event as a JSON file into `/var/spool/sms/webhooks` (`WEBHOOK_QUEUE`), so the modem
handler is never blocked; the `smstools-http-api-webhooks` service POSTs the queued events
to every subscribed webhook and retries failed deliveries (30 s, doubling up to 1 h) until
`WEBHOOK_MAX_AGE` (24 h), then moves the event to `webhooks/failed`.

1. `/etc/smsd.conf`, global section:

       eventhandler = /usr/bin/smstools-http-api-event

   (an eventhandler you had before goes to `EVENTHANDLER_CHAIN` in `settings.py`; it is
   run first with the same arguments), then `systemctl restart smstools`.
   Debian runs smsd as `smsd:dialout`, so the handler usually cannot read `settings.py`
   (`root:smsd 640`); it then queues every event and the worker drops the ones no hook
   subscribed to. `EVENTHANDLER_CHAIN` and a non-default `WEBHOOK_QUEUE` need the settings
   to be readable by the group smsd runs with.
2. `WEBHOOKS` in `/etc/smstools-http-api/settings.py`, see `contrib/settings.py.example`:

       WEBHOOKS = [
           {'name': 'example', 'url': 'https://hooks.example.org/sms',
            'events': ['RECEIVED'], 'headers': {'Authorization': 'Bearer TOKEN'}},
       ]

   Per hook: `events` (`RECEIVED` by default; also `SENT`, `FAILED`, `REPORT`, `CALL`),
   `headers`, `auth` (basic auth tuple), `timeout`, `verify=False`, and the callables
   `filter(event)`, `payload(event)` (request body, default: the event) and
   `check(status, body)` (success test, default: HTTP 2xx). The example file has a
   ready-made hook for the Zabbix 7 API (`history.push` into a trapper item).

   A hook with `'type': 'email'` mails the event instead (text plus the main headers),
   with the same queue and retries:

       {'name': 'mail', 'type': 'email', 'events': ['RECEIVED'],
        'to': 'me@example.org', 'from': 'sms@example.org',
        'smtp_host': 'mail.example.org', 'smtp_port': 25}

   Optional: `starttls`, `smtp_ssl`, `smtp_auth` (user, password), `subject` and `body`
   (strings or callables of the event).
3. `systemctl enable --now smstools-http-api-webhooks` (restart it after changing the settings).

The event that is POSTed by default:

    {
      "event": "RECEIVED",
      "message_id": "GSM1.3GnJtv",
      "kind": "incoming",
      "from": "436761234567",
      "to": null,
      "modem": "GSM1",
      "sms_id": null,
      "text": "Test 3",
      "headers": {"From": "436761234567", "Sent": "26-11-19 11:29:14", "Received": "26-11-19 11:29:25", ...},
      "time": "2026-11-19T10:29:26+00:00"
    }

`smstools-http-api-webhooks --once` delivers what is due and exits (for testing). In a
container, run the worker from the same image with `--entrypoint smstools-http-api-webhooks`;
the eventhandler has to run where smsd runs.

### Routing and mail2sms

With `CONTACTS` (a CardDAV address book or a vCard file, see `contrib/settings.py.example`)
contacts answer to their nicknames, groups (categories), given and full names, mail
addresses and numbers.

*Received SMS* — a webhook of type `route` mails an SMS to the contacts named by its
leading tokens, `@mum @rene dinner at 6`; an SMS without (known) tokens goes to
`default_to`, tokens in `skip` (e.g. `@zabbix`) are left to other hooks. The mail comes
from `<number>@<domain>`, so answering it sends an SMS back.

*Mail to SMS* — `smstools-http-api-mail2sms SENDER RECIPIENT` reads a mail on stdin and
sends its text (without quotes and signature, else the subject) to the number or contact
group in the recipient's local part. Only `MAIL2SMS_SENDERS` and contacts' addresses (in
`MAIL2SMS_SENDER_DOMAINS`) may send; it exits with sysexits codes so the MTA bounces with
a reason. Postfix on the smsd host (`sms.example.org` relayed to it):

    # main.cf
    relay_domains = sms.example.org
    transport_maps = inline:{ sms.example.org=smstools-http-api-mail2sms: }
    smstools-http-api-mail2sms_destination_recipient_limit = 1
    # master.cf
    smstools-http-api-mail2sms unix - n n - 4 pipe
      flags=Rq user=smsd argv=/usr/bin/smstools-http-api-mail2sms ${sender} ${recipient}

### Development

    $ python3 -m venv venv && . venv/bin/activate && pip install -e '.[server]'
    $ smstools-http-api --dev -c development
    $ python3 test.py

API Documentation
-----------------

- `POST /api/v1.0/sms/outgoing`

    Send a new SMS.

    The body must contain a JSON object that defines `mobiles` array and `text` fields. Status code 201 is returned on success. Body of the response contains following JSON object:

    - `message_id` - Mapping of supplied numbers to their corresponding spooler file names (ids).
    - `parts_count` - Tells you how many parts will the SMS be split into.
    - `sent_text` - Original message text.
    - `queue` - Queue to send message. If queue is not set, you can define DEFAULTQUEUE in config.py or keep smsd choose it.

    Status code 400 (Bad Request) is returned in case of a failure to parse input data.

    `demo` is `true` when the API runs in DEMO mode (nothing was handed to smsd).

    If `USER_WHITELIST` option is enabled in the `config.py`, number access control lists are applied.

- `GET /api/v1.0/sms/outgoing`

    Send a new SMS.

    The parameters must contain a `mobiles` (separate each entry with comma) and `text` fields. Status code 201 is returned on success. Body of the response contains following JSON object as describe above.


- `GET /api/v1.0/sms/<kind>/<string:message_id>`

    Get information about a message.

    Where `kind` can be one of `incoming`, `outgoing`, `checked`, `failed`, or `sent`. Body of the response contains a JSON object with all headers and following two fields:

    - `message_id` - Unique identifier of the message.
    - `text` - Text of the message.

    Status code 404 (Not Found) signifies that no message with such an `message_id` has been found.


- `DELETE /api/v1.0/sms/<kind>/<string:message_id>`

    Delete a message.

    Where `kind` can be one of `incoming`, `outgoing`, `checked`, `failed`, or `sent`. Body of the response contains a JSON object with all headers and following two fields:

    - `deleted` - Unique identifier of the message.(<kind>/<message_id>)

    Status code 404 (Not Found) signifies that no message with such an `message_id` has been found.
    Status code 403 (Forbidden) signifies that auth user not found in ADMIN_ACCOUNTS.


- `GET /api/v1.0/sms/<kind>/`

    List messages of given `kind`.

    Where `kind` can be one of `incoming`, `outgoing`, `checked`, `failed`, or `sent`. Body of the response contains a JSON object with a single field:

    - `message_id` - List of identifiers of the messages of given `kind`.
    - `limit` -  Output limit for message_id (false for unlimited).
    - `total_count` - Count of list message_id.

Example
-------

To send an SMS with `curl` installed (POST):

    $ curl -u lvv:SecretPAss -i -H "Content-Type: application/json; charset=UTF-8" -d '{"text":"Hi, Jack!", "mobiles":["79680000000", "79160000000"]}' http://127.0.0.1:5000/api/v1.0/sms/outgoing

Should result in:

    HTTP/1.0 201 CREATED
    Content-Type: application/json

    {
      "mobiles": {
        "79160000000": {
          "message_id": "f22f4f49-eb75-44d8-9690-7ac6d5b607a0",
          "response": "Ok"
        },
        "79680000000": {
          "message_id": "0be8248b-1711-4257-b766-18fcac2babcb",
          "response": "Ok"
        },
      },
      "parts_count": 1,
      "sent_text": "Hi, Jack!"
    }

To send an SMS with `curl` installed (GET):

    $ curl -u lvv:SecretPAss -i -H "Content-Type: application/json; charset=UTF-8" 'http://127.0.0.1:5000/api/v1.0/sms/outgoing?text=Hi+Jack!&mobiles=79680000000,79160000000'

To inquire about a sent SMS:

    $ curl -u lvv:SecretPAss -i -H "Content-Type: application/json; charset=UTF-8" http://127.0.0.1:5000/api/v1.0/sms/sent/83c201e0-c093-47ee-9ab1-e968cbc58446

Should result in:

    HTTP/1.0 200 OK
    Content-Type: application/json

    {
      "From": "lvv",
      "Sent": "14-10-13 11:37:09",
      "To": "79160000000",
      "Modem": "GSM1",
      "IMSI": "230000000000000",
      "message_id": "83c201e0-c093-47ee-9ab1-e968cbc58446"
      "text": "Hi, Jack!",
    }

Using a wrong `message_id`:

    $ curl -u lvv:SecretPAss -i -H "Content-Type: application/json; charset=UTF-8" http://127.0.0.1:5000/api/v1.0/sms/sent/7b533050-d5a0-4b52-b8ba-f5d2ed42e630f

Should result in:

    HTTP/1.0 404 NOT FOUND
    Content-Type: application/json

    {
      "message: ": "Not found: http://127.0.0.1:5000/api/v1.0/sms/outgoing/7b533050-d5a0-4b52-b8ba-f5d2ed42e630f",
      "status: ": 404
    }

Listing all received messages:

    $ curl -u lvv:SecretPAss -i -H "Content-Type: application/json; charset=UTF-8" http://127.0.0.1:5000/api/v1.0/sms/sent/

Should result in:

    HTTP/1.0 200 OK
    Content-Type: application/json

    {
      "limit": 10000,
      "message_id": ["feabbf50-0a55-488e-af0c-026cd3a5d10d", "4935165a-606d-450e-ad91-0086483e4ecc"],
      "total_count": 2
    }

Link to the APIARY doc:
[https://smstoolshttpapi.docs.apiary.io](https://smstoolshttpapi.docs.apiary.io)

And that is all.

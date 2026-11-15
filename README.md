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

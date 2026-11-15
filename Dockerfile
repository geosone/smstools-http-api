# smstools-http-api as a container. smsd (SMS Server Tools 3) runs on the host
# or in another container; share its spool directory and run the API with the
# uid/gid that owns it, e.g.:
#   docker run -d -p 5000:5000 --user "$(id -u smsd):$(id -g smsd)" \
#     -v /var/spool/sms:/var/spool/sms -v /etc/smstools-http-api:/etc/smstools-http-api:ro \
#     ghcr.io/geosone/smstools-http-api
FROM python:3.13-slim

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY smstools_http_api ./smstools_http_api
RUN pip install --no-cache-dir '.[server]' && rm -rf /src

ENV SMSTOOLS_HTTP_API_BIND=0.0.0.0:5000 \
    SMSTOOLS_HTTP_API_SETTINGS=/etc/smstools-http-api/settings.py
VOLUME ["/var/spool/sms", "/etc/smstools-http-api"]
EXPOSE 5000
USER 65534:65534
ENTRYPOINT ["smstools-http-api"]

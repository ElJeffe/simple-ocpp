# simple-ocpp: OCPP 1.6-J central system with a web interface.
#
#   docker build -t simple-ocpp .
#   docker run -d --name simple-ocpp -p 9000:9000 -p 8090:8090 \
#     -v simple-ocpp-data:/var/lib/simple-ocpp -e TZ=Europe/Amsterdam simple-ocpp
#
# Or use compose.yaml. See "Running in Docker" in README.md.

FROM python:3.14-slim

# tzdata: energy is logged per local day, so the container needs to know the
# time zone (set TZ when running it).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user that owns only the data directory.
RUN useradd --system --uid 10001 --home-dir /var/lib/simple-ocpp --shell /usr/sbin/nologin simple-ocpp \
 && mkdir -p /var/lib/simple-ocpp \
 && chown simple-ocpp:simple-ocpp /var/lib/simple-ocpp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OCPP_DATA_DIR=/var/lib/simple-ocpp \
    # Inside the container the server always listens on these; choose the
    # ports on the host with -p instead. Setting them here also locks the
    # network fields on the settings page, where a change would break the
    # port mapping.
    OCPP_HOST=0.0.0.0 \
    OCPP_PORT=9000 \
    OCPP_HTTP_HOST=0.0.0.0 \
    OCPP_HTTP_PORT=8090

USER simple-ocpp
VOLUME ["/var/lib/simple-ocpp"]
EXPOSE 9000 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['OCPP_HTTP_PORT'] + '/api/auth', timeout=4)"

# The server stops cleanly on SIGTERM (docker stop), disconnecting chargers
# properly; it allows connections 10 seconds to close.
STOPSIGNAL SIGTERM
CMD ["python", "src/main.py"]

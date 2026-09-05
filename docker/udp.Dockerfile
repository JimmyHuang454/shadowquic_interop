FROM python:3.13-alpine

COPY docker/udp/udp.py /usr/local/bin/udp.py
RUN chmod 0755 /usr/local/bin/udp.py

ENTRYPOINT ["/usr/local/bin/udp.py"]

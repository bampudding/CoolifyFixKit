FROM alpine:3.24

RUN apk add --no-cache docker-cli curl bash jq python3 incus-client

WORKDIR /app

COPY check_and_heal.sh /app/check_and_heal.sh
COPY resource_guard.py /app/resource_guard.py
RUN chmod +x /app/check_and_heal.sh

CMD ["/bin/bash", "/app/check_and_heal.sh"]

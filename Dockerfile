FROM alpine:latest

# 필요한 도구 설치 (docker-cli, curl, bind-tools/dig, bash, jq)
RUN apk add --no-cache docker-cli curl bind-tools bash jq

WORKDIR /app

COPY check_and_heal.sh /app/check_and_heal.sh
RUN chmod +x /app/check_and_heal.sh

CMD ["/bin/bash", "/app/check_and_heal.sh"]

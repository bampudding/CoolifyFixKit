"""Best-effort local event delivery; never delays a guard on network access."""
import http.client
import json
from pathlib import Path
import socket

SOCKET='/run/coolify-fixkit/notify.sock'


def post_event(kind,scope,message):
    if not Path(SOCKET).exists():return False
    connection=http.client.HTTPConnection('localhost',timeout=.3)
    try:
        connection.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        connection.sock.settimeout(.3);connection.sock.connect(SOCKET)
        connection.request('POST','/events',json.dumps({'kind':kind,'scope':scope,'message':message}),{'Content-Type':'application/json'})
        return connection.getresponse().status in (200,202)
    except (OSError,http.client.HTTPException):return False
    finally:connection.close()

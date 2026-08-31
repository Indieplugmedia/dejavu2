#!/bin/sh
cd /app
export ASPNETCORE_HTTP_PORTS=3340
export ASPNETCORE_URLS=http://127.0.0.1:3340
./Emy --base-directory ./data &
EMYPID=$!
sleep 8
socat TCP-LISTEN:${PORT:-8080},fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:3340 &
wait "$EMYPID"

#!/bin/sh
set -eu

# Archive metadata, imported media and SQLite state are private by default.
umask 077

case "${APP_BIND_ADDRESS:-127.0.0.1}" in
  127.0.0.1|::1) ;;
  *) echo "APP_BIND_ADDRESS must remain loopback-only" >&2; exit 1 ;;
esac

if [ "${TMPDIR:-}" = "/app/data/provider-staging/.runtime-tmp" ]; then
  rm -rf -- /app/data/provider-staging/.runtime-tmp
  mkdir -p /app/data/provider-staging/.runtime-tmp
  chmod 0700 /app/data/provider-staging/.runtime-tmp
fi

exec "$@"

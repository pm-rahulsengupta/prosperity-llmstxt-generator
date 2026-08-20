#!/bin/sh
set -e

# Railway builds the final stage of a Dockerfile and offers no way to select a
# target, but this repo needs both a web and a worker runtime from one image. The
# final stage therefore carries both and dispatches here on APP_TARGET. Set
# APP_TARGET=web or APP_TARGET=worker per Railway service.
#
# When APP_TARGET is unset the script execs its arguments unchanged, which is the
# docker-compose path: compose *can* select a target and passes an explicit CMD.
#
# RUN_MIGRATIONS=true runs Alembic before starting. Railway has no init containers,
# so the alternative is a human remembering to migrate after every deploy. Set it on
# exactly one service: two replicas with it enabled means two migrators racing the
# same advisory lock.

if [ "$RUN_MIGRATIONS" = "true" ]; then
	echo "[entrypoint] running database migrations"
	(cd /app && alembic upgrade head)
	echo "[entrypoint] migrations complete"
fi

case "$APP_TARGET" in
	web)
		echo "[entrypoint] starting web on port ${PORT:-3000}"
		exec python -m app.web
		;;
	worker)
		echo "[entrypoint] starting worker"
		exec python -m app.jobs.worker
		;;
	migrate)
		if [ "$RUN_MIGRATIONS" != "true" ]; then
			exec alembic upgrade head
		fi
		echo "[entrypoint] migrations already run; nothing to do"
		exit 0
		;;
	"")
		exec "$@"
		;;
	*)
		echo "[entrypoint] unknown APP_TARGET '$APP_TARGET' (expected web, worker or migrate)" >&2
		exit 1
		;;
esac

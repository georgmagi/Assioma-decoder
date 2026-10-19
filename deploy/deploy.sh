#!/bin/sh
# Ship the app's code to the server and restart the Assioma service (run from the repo folder).
# The server keeps no git: just these files. Data and secrets on the server are never touched.
set -eu
HOST=${1:-dt}
cd "$(dirname "$0")/.."
rsync -az --delete --chmod=D755,F644 --include='*.py' --include='web/***' --include='requirements-server.txt' --exclude='*' \
  ./ "$HOST":/tmp/assioma-app/
ssh "$HOST" 'sudo rsync -a --delete /tmp/assioma-app/ /srv/assioma/app/ && sudo chown -R root:root /srv/assioma/app \
  && sudo /srv/assioma/venv/bin/pip install -q -r /srv/assioma/app/requirements-server.txt \
  && sudo systemctl restart assioma && sleep 2 && systemctl is-active assioma && curl -fsS http://127.0.0.1:8100/healthz && echo'

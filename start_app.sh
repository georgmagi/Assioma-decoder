#!/usr/bin/env bash
# Assioma Recorder (Linux): run ./start_app.sh or pick "Assioma Recorder" from the app menu.
cd "$(dirname "$(readlink -f "$0")")"
exec .venv/bin/python recorder_app.py "$@"

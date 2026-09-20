#!/bin/bash
set -e
REAL_GIT="$(cat /usr/local/share/recreationbench/real-git-path)"
exec "$REAL_GIT" "$@"

#!/bin/bash
case "$1" in
    clone|fetch|pull|remote)
        echo "BLOCKED: git $1 is not allowed" >&2
        exit 1
        ;;
    *) exec /usr/local/bin/git.real "$@" ;;
esac

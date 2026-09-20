#!/bin/bash
tool="$(basename "$0")"
echo "BLOCKED: $tool is not allowed during recreation" >&2
exit 1

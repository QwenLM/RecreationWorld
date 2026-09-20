#!/bin/bash
export CUA_DRIVER_RS_COORDINATE_SPACE=1
export CUA_DRIVER_RS_COORDINATE_SCALE=1000
exec /opt/cua-driver-real "$@"

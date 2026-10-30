#!/bin/sh
set -eu
export PYTHONPATH=/opt/registry
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export TZ=Asia/Shanghai
umask 077
exec python3 -m ha_artifact_registry.main

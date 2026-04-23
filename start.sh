#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

source .venv/bin/activate
go2-rebot "$@"

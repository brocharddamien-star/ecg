#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/polar_app.py"

#!/usr/bin/env bash
set -euo pipefail

exec uv run news-collector cnyes-backfill "$@"

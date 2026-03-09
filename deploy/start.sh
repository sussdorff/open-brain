#!/bin/bash
# Start open-brain with secrets from 1Password
# Secrets are injected as env vars via op run — never written to disk
set -euo pipefail

cd /opt/open-brain/python

export OP_SERVICE_ACCOUNT_TOKEN=$(cat /etc/op-service-account-token)

exec op run --env-file=../.env.tpl -- uv run python -m open_brain

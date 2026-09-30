#!/usr/bin/env bash
# Run a command, and if it fails, try again: three attempts, 30 seconds apart.
#
# For the workflows' install steps. pip already retries a dropped connection,
# but only for a few seconds; a PyPI or PyTorch-index outage that lasts longer
# fails the whole job, and a failed trade-bot job is a run that places nothing.
#
#   bash scripts/retry.sh pip install -r requirements-bot.txt
#
# RETRY_ATTEMPTS and RETRY_DELAY override the defaults (the tests use a delay
# of 0). Exits with the command's own status from the last attempt.

attempts=${RETRY_ATTEMPTS:-3}
delay=${RETRY_DELAY:-30}

for ((i = 1; i <= attempts; i++)); do
  "$@" && exit 0
  status=$?
  if ((i < attempts)); then
    echo "::warning::'$*' failed (exit $status), attempt $i of $attempts; retrying in ${delay}s" >&2
    sleep "$delay"
  fi
done

echo "::error::'$*' failed $attempts times (last exit $status)" >&2
exit "$status"

#!/bin/bash
set -euo pipefail

REGION="${region}"
JOIN_PARAMETER_NAME="/moataz-k8s/join-command"

echo "Starting Kubernetes Worker initialization..."

systemctl enable --now crio
systemctl enable --now kubelet

MAX_ATTEMPTS=20
SLEEP_SECONDS=15

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  echo "Attempt $attempt/$MAX_ATTEMPTS: fetching join command from SSM..."

  JOIN_COMMAND=$(aws ssm get-parameter \
    --name "$${JOIN_PARAMETER_NAME}" \
    --with-decryption \
    --region "$${REGION}" \
    --query "Parameter.Value" \
    --output text 2>/dev/null || true)

  if [ -z "$${JOIN_COMMAND}" ] || [ "$${JOIN_COMMAND}" = "None" ]; then
    echo "Join command not available yet. Waiting $SLEEP_SECONDS seconds..."
    sleep "$SLEEP_SECONDS"
    continue
  fi

  echo "Join command found. Attempting to join..."

  if timeout 60 bash -c "$${JOIN_COMMAND}"; then
    echo "Successfully joined the Kubernetes cluster."
    exit 0
  fi

  echo "Join failed or timed out. Cleaning up and retrying..."

  kubeadm reset -f || true

  sleep "$SLEEP_SECONDS"
done

echo "ERROR: Failed to join the cluster after $MAX_ATTEMPTS attempts."
exit 1
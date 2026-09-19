#!/bin/sh
set -eu

workspace=${WORKSPACE:-/workspace}
state_root=${STATE_ROOT:-/state}
control_container_id=${CONTROL_CONTAINER_ID:-}
if [ -z "$control_container_id" ]; then
    IFS= read -r control_container_id </proc/sys/kernel/hostname
fi
image_id=$(docker build --quiet --tag sang-coding-agent-sandbox:local "$workspace/sandbox-image")
case "$image_id" in
    sha256:????????????????????????????????????????????????????????????????) ;;
    *) echo "sandbox build did not return an immutable image ID" >&2; exit 1 ;;
esac
exec coding-agent --workspace "$workspace" --state-root "$state_root" --image "$image_id" --control-container-id "$control_container_id" "$@"

#!/usr/bin/env bash
# Build one component image for linux/amd64, tagged with its version from deploy/versions.json.
#   deploy/build.sh <broker|planner|api|worker>
# Prints the image reference. Pushing to ECR is done by the release workflow.
# IMAGE_NAMESPACE overrides the repository namespace (default aws-inventory-discover).
set -euo pipefail

component="${1:?usage: deploy/build.sh <broker|planner|api|worker>}"
case "$component" in broker | planner | api | worker) ;; *)
  echo "unknown component: $component" >&2
  exit 2
  ;;
esac

root="$(cd "$(dirname "$0")/.." && pwd)"
version="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' \
  "$root/deploy/versions.json" "$component")"
image="${IMAGE_NAMESPACE:-aws-inventory-discover}/${component}:${version}"

podman build \
  --platform linux/amd64 \
  --file "$root/deploy/Containerfile" \
  --target "$component" \
  --label "org.opencontainers.image.version=${version}" \
  --label "org.opencontainers.image.revision=$(git -C "$root" rev-parse HEAD 2>/dev/null || echo unknown)" \
  --tag "$image" \
  "$root" >&2

echo "$image"

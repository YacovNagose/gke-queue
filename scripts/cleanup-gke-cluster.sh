#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
CLUSTER_NAME="${CLUSTER_NAME:-gke-kueue-gpu-demo}"
REGION="${REGION:-asia-southeast1}"
MODE="${1:---jobs-only}"

if [[ "${MODE}" == "--all" ]]; then
  echo "Deleting entire GKE cluster '${CLUSTER_NAME}' in ${REGION} (project: ${PROJECT_ID})..."
  gcloud container clusters delete "${CLUSTER_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --quiet
  echo "Cluster deleted."
else
  echo "Cleaning up all demo Kubernetes Jobs in namespace 'default'..."
  kubectl delete jobs -n default -l "kueue.x-k8s.io/queue-name=training-queue" --ignore-not-found=true
  echo "Demo jobs cleaned up. (Pass '--all' to delete the GKE cluster itself.)"
fi

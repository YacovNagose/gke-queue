#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# GKE + Kueue + Custom Compute Class (L4/T4 Time-Sharing) Demo Setup Script
# Region: Singapore (asia-southeast1) | Dataplane V2 | Rapid Channel (1.35.2+)
# ==============================================================================

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
CLUSTER_NAME="${CLUSTER_NAME:-gke-kueue-gpu-demo}"
REGION="${REGION:-asia-southeast1}"
NETWORK_NAME="${NETWORK_NAME:-gke-kueue-vpc}"
SUBNET_NAME="${SUBNET_NAME:-gke-singapore}"
SUBNET_RANGE="${SUBNET_RANGE:-10.20.0.0/22}"
RELEASE_CHANNEL="${RELEASE_CHANNEL:-rapid}"
KUEUE_VERSION="${KUEUE_VERSION:-v0.10.1}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: No GCP PROJECT_ID detected. Export PROJECT_ID=<your-gcp-project> and retry."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=================================================================="
echo " Provisioning GKE Standard Cluster for Kueue + GPU Sharing Demo"
echo "------------------------------------------------------------------"
echo " Project ID      : ${PROJECT_ID}"
echo " Cluster Name    : ${CLUSTER_NAME}"
echo " Region          : ${REGION} (Singapore)"
echo " VPC / Subnet    : ${NETWORK_NAME} / ${SUBNET_NAME} (${SUBNET_RANGE})"
echo " Release Channel : ${RELEASE_CHANNEL} (GKE 1.35.2+ for priorityScore & gpuSharing)"
echo " Network         : Dataplane V2 (--enable-dataplane-v2)"
echo "=================================================================="

# 1. Enable required Google Cloud APIs & ensure VPC subnet exists in Singapore
echo "[1/5] Ensuring APIs and Singapore VPC subnet '${SUBNET_NAME}' exist..."
gcloud services enable container.googleapis.com compute.googleapis.com \
  --project="${PROJECT_ID}"

if ! gcloud compute networks describe "${NETWORK_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute networks create "${NETWORK_NAME}" \
    --project="${PROJECT_ID}" \
    --subnet-mode=custom
fi

if ! gcloud compute networks subnets describe "${SUBNET_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute networks subnets create "${SUBNET_NAME}" \
    --project="${PROJECT_ID}" \
    --network="${NETWORK_NAME}" \
    --region="${REGION}" \
    --range="${SUBNET_RANGE}" \
    --enable-private-ip-google-access
fi

# 2. Create GKE Standard cluster with Dataplane V2 in Singapore (asia-southeast1)
if gcloud container clusters describe "${CLUSTER_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "[2/5] Cluster '${CLUSTER_NAME}' already exists in ${REGION}. Skipping cluster creation."
else
  echo "[2/5] Creating GKE Standard cluster '${CLUSTER_NAME}' in ${REGION} with Dataplane V2..."
  gcloud container clusters create "${CLUSTER_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --network="${NETWORK_NAME}" \
    --subnetwork="${SUBNET_NAME}" \
    --release-channel="${RELEASE_CHANNEL}" \
    --enable-dataplane-v2 \
    --machine-type="e2-standard-4" \
    --disk-size=50 \
    --num-nodes=1 \
    --enable-autoscaling \
    --min-nodes=1 \
    --max-nodes=3 \
    --scopes="https://www.googleapis.com/auth/cloud-platform"
fi

# 3. Configure kubectl context
echo "[3/5] Fetching kubeconfig credentials for '${CLUSTER_NAME}'..."
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}"

# 4. Install Kueue
echo "[4/5] Installing Kueue (${KUEUE_VERSION})..."
kubectl apply --server-side -f \
  "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/manifests.yaml"

echo "Waiting for kueue-controller-manager to become ready..."
kubectl wait --for=condition=Available deployment/kueue-controller-manager \
  -n kueue-system --timeout=300s

# 5. Apply Custom ComputeClass and Kueue Priority / ClusterQueue manifests
echo "[5/5] Applying Custom ComputeClass (L4/T4 On-Demand & Spot + TIME_SHARING) and Kueue resources..."
kubectl apply -f "${REPO_ROOT}/k8s/01-compute-class.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/02-kueue-config.yaml"

echo ""
echo "=================================================================="
echo " Setup Complete!"
echo "------------------------------------------------------------------"
echo " ComputeClass : cheap-gpu-timeshared (priorityScore 20=On-Demand, 10=Spot, 4x TIME_SHARING)"
echo " ClusterQueue : gpu-cluster-queue (Quota: 4 shared GPU slices, Non-Preemptive)"
echo " LocalQueue   : training-queue (namespace: default)"
echo " Next Step    : Run 'make run-ui' to launch the Demo Mission Control UI on http://127.0.0.1:8000"
echo "=================================================================="

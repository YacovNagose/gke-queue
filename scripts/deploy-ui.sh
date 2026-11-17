#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Build & Deploy the GKE Queue UI in-cluster (Artifact Registry + Cloud Build)
# ==============================================================================

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-asia-southeast1}"
AR_REPO="${AR_REPO:-gke-queue}"
IMAGE_NAME="${IMAGE_NAME:-gke-queue-ui}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: No GCP PROJECT_ID detected. Export PROJECT_ID=<your-gcp-project> and retry."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "=================================================================="
echo " Building & Deploying GKE Queue UI In-Cluster"
echo "------------------------------------------------------------------"
echo " Project ID : ${PROJECT_ID}"
echo " Region     : ${REGION}"
echo " Image URI  : ${IMAGE_URI}"
echo "=================================================================="

# 1. Enable Cloud Build and Artifact Registry APIs
echo "[1/4] Enabling Artifact Registry & Cloud Build APIs..."
gcloud services enable artifactregistry.googleapis.com cloudbuild.googleapis.com \
  --project="${PROJECT_ID}"

# 2. Ensure Artifact Registry Docker repository exists
if ! gcloud artifacts repositories describe "${AR_REPO}" \
  --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "[2/4] Creating Artifact Registry repository '${AR_REPO}' in ${REGION}..."
  gcloud artifacts repositories create "${AR_REPO}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --repository-format=docker \
    --description="Container images for GKE Queue demo"
else
  echo "[2/4] Artifact Registry repository '${AR_REPO}' already exists."
fi

# 3. Build and push image using Cloud Build (no local Docker daemon required)
echo "[3/4] Building container image with Cloud Build..."
gcloud builds submit "${REPO_ROOT}" \
  --project="${PROJECT_ID}" \
  --tag="${IMAGE_URI}"

# 4. Ensure CSRF Secret exists in cluster and deploy UI + ServiceAccount/RBAC
echo "[4/4] Applying CSRF Secret, ServiceAccount, RBAC, Deployment, and ClusterIP Service..."
if ! kubectl get secret gke-queue-ui-secret -n default >/dev/null 2>&1; then
  CSRF_HEX="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  kubectl create secret generic gke-queue-ui-secret \
    -n default \
    --from-literal="csrf-secret=${CSRF_HEX}"
fi

kubectl apply -f "${REPO_ROOT}/k8s/04-ui-deployment.yaml"
kubectl set image deployment/gke-queue-ui "ui=${IMAGE_URI}" -n default
kubectl rollout status deployment/gke-queue-ui -n default --timeout=180s

echo ""
echo "=================================================================="
echo " UI Deployed In-Cluster!"
echo "------------------------------------------------------------------"
echo " ServiceAccount : gke-queue-ui (least-privilege RBAC for Jobs/Pods)"
echo " Service        : svc/gke-queue-ui (ClusterIP)"
echo " Access UI      : Run 'make port-forward-ui' and open http://127.0.0.1:8000"
echo "=================================================================="

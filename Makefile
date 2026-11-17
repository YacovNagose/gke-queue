.PHONY: help install run-ui setup-gke apply-k8s clean-jobs destroy-gke

PYTHON ?= python3
VENV_DIR ?= .venv
HOST ?= 127.0.0.1
PORT ?= 8000

help:
	@echo "=================================================================="
	@echo " GKE + Kueue + Custom Compute Class (L4/T4 Time-Sharing) Demo"
	@echo "=================================================================="
	@echo "  make run-ui       - Install Python deps in .venv and start UI on http://127.0.0.1:8000"
	@echo "  make setup-gke    - Provision GKE Standard (Dataplane V2, asia-southeast1) + Kueue + ComputeClass"
	@echo "  make apply-k8s    - Apply ComputeClass and Kueue manifests to current kubectl context"
	@echo "  make clean-jobs   - Delete all demo training jobs in the GKE cluster"
	@echo "  make destroy-gke  - Tear down the GKE cluster in asia-southeast1"

install:
	@if [ ! -d "$(VENV_DIR)" ]; then $(PYTHON) -m venv $(VENV_DIR); fi
	@$(VENV_DIR)/bin/pip install --quiet --upgrade pip
	@$(VENV_DIR)/bin/pip install --quiet -r requirements.txt

run-ui: install
	@echo "Starting GKE + Kueue Demo UI on http://$(HOST):$(PORT) ..."
	@$(VENV_DIR)/bin/uvicorn app.main:app --host $(HOST) --port $(PORT)

setup-gke:
	@chmod +x scripts/setup-gke-cluster.sh
	@./scripts/setup-gke-cluster.sh

apply-k8s:
	kubectl apply -f k8s/01-compute-class.yaml
	kubectl apply -f k8s/02-kueue-config.yaml

clean-jobs:
	@chmod +x scripts/cleanup-gke-cluster.sh
	@./scripts/cleanup-gke-cluster.sh --jobs-only

destroy-gke:
	@chmod +x scripts/cleanup-gke-cluster.sh
	@./scripts/cleanup-gke-cluster.sh --all

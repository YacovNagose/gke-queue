.PHONY: help install run-ui setup-gke deploy-ui port-forward-ui apply-k8s clean-jobs destroy-gke

PYTHON ?= python3
VENV_DIR ?= .venv
HOST ?= 127.0.0.1
PORT ?= 8000

help:
	@echo "=================================================================="
	@echo " GKE + Kueue + Custom Compute Class (L4/T4 Time-Sharing) Demo"
	@echo "=================================================================="
	@echo "  make run-ui          - Start UI locally on http://127.0.0.1:8000 (Simulation or local kubeconfig)"
	@echo "  make setup-gke       - Provision GKE Standard (Dataplane V2, asia-southeast1) + Kueue + ComputeClass"
	@echo "  make deploy-ui       - Build & deploy UI in-cluster with ServiceAccount/RBAC + ClusterIP Service"
	@echo "  make port-forward-ui - Port-forward in-cluster svc/gke-queue-ui to http://127.0.0.1:8000"
	@echo "  make apply-k8s       - Apply ComputeClass and Kueue manifests to current kubectl context"
	@echo "  make clean-jobs      - Delete all demo training jobs in the GKE cluster"
	@echo "  make destroy-gke     - Tear down the GKE cluster in asia-southeast1"

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

deploy-ui:
	@chmod +x scripts/deploy-ui.sh
	@./scripts/deploy-ui.sh

port-forward-ui:
	@$(PYTHON) -c 'import socket,sys; s=socket.socket(); busy=(s.connect_ex(("$(HOST)", $(PORT)))==0); s.close(); sys.exit("ERROR: $(HOST):$(PORT) is already in use. Stop it or run: make port-forward-ui PORT=8080") if busy else None'
	@echo "Forwarding in-cluster UI to http://$(HOST):$(PORT) (Ctrl+C to stop)..."
	@trap 'exit 0' INT TERM; while true; do \
		kubectl port-forward svc/gke-queue-ui $(PORT):80 -n default --address $(HOST) || true; \
		sleep 1; \
	done

apply-k8s:
	kubectl apply -f k8s/01-compute-class.yaml
	kubectl apply -f k8s/02-kueue-config.yaml

clean-jobs:
	@chmod +x scripts/cleanup-gke-cluster.sh
	@./scripts/cleanup-gke-cluster.sh --jobs-only

destroy-gke:
	@chmod +x scripts/cleanup-gke-cluster.sh
	@./scripts/cleanup-gke-cluster.sh --all

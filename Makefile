.PHONY: install lint typecheck test eval eval-bedrock eval-azure eval-gcp eval-compare env \
        deploy-aws deploy-azure deploy-gcp plan-aws plan-azure plan-gcp \
        clean build

# Use bash explicitly so `source` works in recipe lines (default /bin/sh on
# Ubuntu uses dash, which doesn't support `source`).
SHELL := /bin/bash

# Use python3 by default (WSL/Ubuntu ship a non-executable `python` stub that
# breaks `make eval` with "Permission denied"). Override with `make PYTHON=python`
# if your environment puts the interpreter at `python`.
PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip

# scripts/load-env.sh aggregates env vars from .env + tfvars defaults +
# Azure Key Vault for the eval adapters (AWS Bedrock / Azure OpenAI / Vertex).
# Every eval target sources it first so `make eval-*` "just works" from bash.
LOAD_ENV := source scripts/load-env.sh &&

# ── Local dev ─────────────────────────────────────────────────────────────────

# `install` pulls in BOTH runtime deps (boto3, openai, vertexai, azure-cosmos…)
# AND dev deps (pytest, mypy, ruff). The runtime deps are needed by the eval
# adapters (vertex / bedrock / azure_openai) when running evals locally.
install:
	$(PIP) install -r requirements.txt
	$(PIP) install -r requirements-dev.txt

lint:
	ruff check src/ evals/ tests/

typecheck:
	mypy src/ --ignore-missing-imports

test:
	pytest tests/unit/ -v --tb=short

test-integration:
	pytest tests/integration/ -v --tb=short

test-e2e:
	pytest tests/e2e/ -v --tb=short

# ── Evals ─────────────────────────────────────────────────────────────────────

eval:
	# Mock is a keyword classifier — its ceiling is ~65%. We use this as a
	# "pipeline is wired up" smoke test, not a model quality check.
	$(LOAD_ENV) $(PYTHON) -m evals.run_evals --adapter mock --threshold 0.50

eval-bedrock:
	$(LOAD_ENV) $(PYTHON) -m evals.run_evals --adapter bedrock --threshold 0.80

eval-azure:
	$(LOAD_ENV) $(PYTHON) -m evals.run_evals --adapter azure_openai --threshold 0.80

eval-gcp:
	$(LOAD_ENV) $(PYTHON) -m evals.run_evals --adapter vertex --threshold 0.80

eval-compare:
	$(LOAD_ENV) $(PYTHON) -m evals.compare_models

# Diagnostic — show what env vars the eval targets will run with (masked).
env:
	@$(LOAD_ENV) bash -c ' \
	  mask() { v="$$1"; [[ -z "$$v" ]] && echo "(unset)" || { [[ $${#v} -gt 12 ]] && echo "$${v:0:4}…$${v: -4} ($${#v} chars)" || echo "*** ($${#v} chars)"; }; }; \
	  echo "AWS_REGION                  = $$AWS_REGION"; \
	  echo "AWS_ACCESS_KEY_ID           = $$(mask $$AWS_ACCESS_KEY_ID)"; \
	  echo "AWS_SECRET_ACCESS_KEY       = $$(mask $$AWS_SECRET_ACCESS_KEY)"; \
	  echo "BEDROCK_MODEL_ID            = $$BEDROCK_MODEL_ID"; \
	  echo "AZURE_OPENAI_ENDPOINT       = $$AZURE_OPENAI_ENDPOINT"; \
	  echo "AZURE_OPENAI_DEPLOYMENT     = $$AZURE_OPENAI_DEPLOYMENT"; \
	  echo "AZURE_OPENAI_API_KEY        = $$(mask $$AZURE_OPENAI_API_KEY)"; \
	  echo "AZURE_OPENAI_API_VERSION    = $$AZURE_OPENAI_API_VERSION"; \
	  echo "GOOGLE_CLOUD_PROJECT        = $$GOOGLE_CLOUD_PROJECT"; \
	  echo "VERTEX_AI_LOCATION          = $$VERTEX_AI_LOCATION"; \
	  echo "VERTEX_MODEL_ID             = $$VERTEX_MODEL_ID"; \
	  echo "DD_SITE                     = $$DD_SITE"; \
	  echo "DD_API_KEY                  = $$(mask $$DD_API_KEY)"; \
	'

# ── Build lambda/function zip ─────────────────────────────────────────────────

build:
	mkdir -p .build
	pip install -r requirements.txt -t .build/deps/ --quiet
	cd .build/deps && zip -r ../lambda.zip . -x "*.pyc" -x "__pycache__/*" > /dev/null
	zip -r .build/lambda.zip src/ -x "*.pyc" -x "__pycache__/*"
	cp .build/lambda.zip .build/function_source.zip
	@echo "Built .build/lambda.zip and .build/function_source.zip"

clean:
	rm -rf .build/ __pycache__/ .mypy_cache/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ── Terraform — plan ──────────────────────────────────────────────────────────

plan-aws:
	cd infra/environments/aws-dev && terraform init && terraform plan

plan-azure:
	cd infra/environments/azure-dev && terraform init && terraform plan

plan-gcp:
	cd infra/environments/gcp-dev && terraform init && terraform plan

# ── Terraform — deploy (no auto-approve — review plan output first) ───────────

deploy-aws: build
	cd infra/environments/aws-dev && terraform init && terraform apply

deploy-azure: build
	cd infra/environments/azure-dev && terraform init && terraform apply

deploy-gcp: build
	cd infra/environments/gcp-dev && terraform init && terraform apply

# ── Terraform — destroy ───────────────────────────────────────────────────────

destroy-aws:
	cd infra/environments/aws-dev && terraform destroy

destroy-azure:
	cd infra/environments/azure-dev && terraform destroy

destroy-gcp:
	cd infra/environments/gcp-dev && terraform destroy

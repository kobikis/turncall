.PHONY: help install dev test test-unit test-integration lint format typecheck security run migrate migrate-new docker-up docker-down docker-all docker-up-local docker-down-local migrate-local clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	pip install -e .

dev: ## Install dev dependencies
	pip install -e ".[dev]"

test: ## Run tests with coverage
	pytest --cov=src/turncall --cov-report=term-missing -v

test-unit: ## Run unit tests only
	pytest -m unit --cov=src/turncall --cov-report=term-missing -v

test-integration: ## Run integration tests only
	pytest -m integration -v

lint: ## Run linter
	ruff check src/ tests/

format: ## Format code
	ruff check --fix src/ tests/
	ruff format src/ tests/

typecheck: ## Run type checking
	mypy src/turncall/

security: ## Run security scan
	bandit -r src/ -c pyproject.toml 2>/dev/null || bandit -r src/

run: ## Run the development server
	python -m turncall

migrate: ## Run database migrations (in docker; needs `make docker-up` first)
	cd localstack && docker compose run --rm --no-deps -v "$(CURDIR)/alembic:/app/alembic" turncall alembic upgrade head

migrate-new: ## Create a new migration (usage: make migrate-new msg="description"; needs `make docker-up` first)
	cd localstack && docker compose run --rm --no-deps -v "$(CURDIR)/alembic:/app/alembic" turncall alembic revision --autogenerate -m "$(msg)"

gen-openapi: ## Regenerate docs/openapi.json from the app spec
	python scripts/gen_openapi.py

check-openapi: ## Fail if docs/openapi.json is stale (CI guard)
	python scripts/gen_openapi.py --check

SKILL_REPO ?= ../turncall-skill
sync-skill: gen-openapi ## Regenerate docs spec + copy it into the turncall-skill checkout (SKILL_REPO=path)
	python scripts/sync_skill.py --skill-repo $(SKILL_REPO)

docker-up: ## Start local stack (postgres + redis + turncall api + localstack)
	# --build: src/ is bind-mounted so code is live, but a dependency change needs
	# the image rebuilt — without it you get an ImportError against the old venv.
	cd localstack && docker compose up -d --build

docker-down: ## Stop local infrastructure
	cd localstack && docker compose down --volumes

LOCAL_COMPOSE = docker compose -f docker-compose.local.yml

docker-up-local: ## Start local-storage stack (postgres + redis + turncall api, NO localstack)
	cd localstack && $(LOCAL_COMPOSE) up -d --build

docker-down-local: ## Stop the local-storage stack
	cd localstack && $(LOCAL_COMPOSE) down --volumes

migrate-local: ## Run migrations against the local-storage stack (needs `make docker-up-local` first)
	cd localstack && $(LOCAL_COMPOSE) run --rm --no-deps -v "$(CURDIR)/alembic:/app/alembic" turncall alembic upgrade head

docker-all: ## Start all services including turncall
	cd localstack && docker compose up -d --build

clean: ## Clean build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ *.egg-info src/*.egg-info

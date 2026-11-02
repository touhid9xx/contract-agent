# Convenience targets. `make` is a POSIX tool and is not present on every
# developer machine (this project's primary host is Windows), so each target
# delegates to a cross-platform Node script. Every command also works directly
# via npm, which is the documented path in README.md.

.PHONY: help bootstrap dev test lint fmt up down logs demo

help:
	@echo "bootstrap  install venv, node deps and git hooks (npm run bootstrap)"
	@echo "dev        run the frontend dev server"
	@echo "test       run backend tests"
	@echo "lint       ruff + mypy + bandit + pre-commit"
	@echo "fmt        ruff format + prettier"
	@echo "up         docker compose up --build"
	@echo "down       docker compose down"
	@echo "demo       run the Phase 0 mocked demo"

bootstrap:
	npm run bootstrap

dev:
	npm run dev

test:
	venv/Scripts/python -m pytest

lint:
	venv/Scripts/ruff check backend
	venv/Scripts/mypy
	venv/Scripts/bandit -c pyproject.toml -r backend/app -q
	venv/Scripts/pre-commit run --all-files

fmt:
	venv/Scripts/ruff format backend
	npm run format

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f api

demo:
	npm run demo

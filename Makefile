.PHONY: setup fmt lint typecheck test test-int cov up down moto migrate image check

setup:
	uv sync

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest

test-int:
	uv run pytest -m integration

cov:
	uv run pytest -m "" --cov=inventory --cov-report=term-missing

up:
	podman compose up -d

down:
	podman compose down

moto:
	uv run moto_server -p 5000

migrate:
	uv run alembic upgrade head

image:
	podman build --platform linux/arm64 -t inventory:dev .

check: lint typecheck test

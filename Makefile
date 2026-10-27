.PHONY: setup fmt lint typecheck test test-int cov up down moto migrate queues images ddl check

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

queues:  # the scan queues in moto, found by name like in AWS (needs make moto; .env exported)
	uv run python -c "import boto3; [boto3.client('sqs', region_name=r, endpoint_url='$${INVENTORY_AWS_ENDPOINT_URL:-http://localhost:5000}').create_queue(QueueName=f'inventory-local-{k}') for r in ('ap-east-1','eu-west-1','eu-west-2','us-east-1') for k in ('scheduled','ondemand')]"

images:
	for c in broker planner api worker; do deploy/build.sh $$c; done

ddl:
	deploy/ddl.sh base head

check: lint typecheck test

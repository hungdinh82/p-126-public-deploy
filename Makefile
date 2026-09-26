.PHONY: run test lint format typecheck check clean

PYTHON ?= .venv/bin/python

run:
	$(PYTHON) -m uvicorn src.vivi.api.app:app --reload --host 127.0.0.1 --port 8787

test:
	$(PYTHON) -m pytest tests/ -v

lint:
	$(PYTHON) -m ruff check server/ src/ vehicle_simulator/ tests/

format:
	$(PYTHON) -m ruff format server/ src/ vehicle_simulator/ tests/

typecheck:
	$(PYTHON) -m mypy src/

check: lint test

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +

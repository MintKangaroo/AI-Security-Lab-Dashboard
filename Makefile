.PHONY: install run test lint check

install:
	python3 -m pip install -e ".[dev]"

run:
	PYTHONPATH=src python3 -m lab_dashboard

test:
	PYTHONPATH=src python3 -m pytest

lint:
	python3 -m ruff check .

check: lint test

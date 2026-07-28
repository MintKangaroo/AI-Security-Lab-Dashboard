.PHONY: install run test lint check

install:
	python3 -m pip install -e ".[dev]"

run:
	python3 -m lab_dashboard

test:
	python3 -m pytest

lint:
	python3 -m ruff check .

check: lint test

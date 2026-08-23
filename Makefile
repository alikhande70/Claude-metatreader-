.PHONY: help install test test-fast lint typecheck dashboard demo clean check

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## install the package and dev tooling
	pip install -e ".[dev]"

test:  ## run the full test suite
	pytest

test-fast:  ## run everything except the slow engine-equivalence run
	pytest -m "not slow"

lint:  ## ruff
	ruff check src tests sidecar --line-length 100

typecheck:  ## mypy
	mypy

dashboard:  ## build the dashboard bundle into the API's static directory
	cd dashboard && npm ci && npm run build

check: lint test  ## what CI should run

demo:  ## a full offline demonstration: config, backtest with baseline, validation
	atlas init --out /tmp/atlas-demo/atlas.json --force
	atlas doctor /tmp/atlas-demo/atlas.json
	atlas backtest /tmp/atlas-demo/atlas.json --bars 60000 --baseline --out /tmp/atlas-demo/runs
	atlas analyse /tmp/atlas-demo/runs/primary
	@echo
	@echo "Serve the dashboard with:"
	@echo "  atlas serve /tmp/atlas-demo/atlas.json --run-dir /tmp/atlas-demo/runs/primary"

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache **/__pycache__ build dist *.egg-info

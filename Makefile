.PHONY: check container-requirements check-container-requirements

container_requirements := streamlit/common/requirements.txt
export_requirements := uv export --locked --no-dev --no-emit-project --no-hashes --no-annotate --no-header

check: check-container-requirements
	uv sync --locked
	uv run --frozen ruff check
	uv run --frozen ruff format --check
	uv run --frozen mypy --strict
	uv run --frozen pytest -m 'not snowflake'

container-requirements:
	$(export_requirements) --output-file $(container_requirements) --quiet

check-container-requirements:
	$(export_requirements) | diff -u $(container_requirements) -

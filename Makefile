.PHONY: test run worker

test:
	pytest -q

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8100 --reload

worker:
	python -m app.worker

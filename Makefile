SERVICE ?= sheepshead-bay
REGION  ?= us-east1
PORT    ?= 8080

.PHONY: install run docker-build docker-run deploy url logs smoke clean

install:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

run:
	.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --reload

docker-build:
	docker build -t $(SERVICE) .

docker-run: docker-build
	docker run --rm -p $(PORT):8080 -e PORT=8080 $(SERVICE)

# THE ONE-LINER: deploy to Cloud Run.
deploy:
	gcloud run deploy $(SERVICE) --source . --region $(REGION) \
		--allow-unauthenticated --port 8080 --memory 512Mi --max-instances 5

url:
	@gcloud run services describe $(SERVICE) --region $(REGION) --format 'value(status.url)'

logs:
	gcloud run services logs tail $(SERVICE) --region $(REGION)

# Curl every route against a running server. BASE=https://... to hit Cloud Run.
smoke:
	@BASE=$${BASE:-http://localhost:$(PORT)}; \
	echo "--> $$BASE/healthz";      curl -fsS "$$BASE/healthz"; echo; \
	echo "--> $$BASE/api/verdict";  curl -fsS "$$BASE/api/verdict" | head -c 400; echo; \
	echo "--> $$BASE/ (HTML)";      curl -fsS -o /dev/null -w 'status=%{http_code} bytes=%{size_download}\n' "$$BASE/"

clean:
	rm -rf .venv __pycache__ app/__pycache__

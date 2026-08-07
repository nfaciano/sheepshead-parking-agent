# Deploy anything to Cloud Run from a Mac — 10 minutes

Written at NYC Vision Hack after hitting every one of these. The eligibility gate is
"deployed on Cloud Run," so do this first and iterate after.

---

## 0. The one rule that breaks most first deploys

**Your app must listen on the port in `$PORT` and bind `0.0.0.0`** — not 3000, not
localhost. Cloud Run injects `PORT` (usually 8080) and routes to it. If you bind
`127.0.0.1` or a hardcoded port, the deploy "succeeds" and every request 404s or hangs.

```python
# Python / FastAPI
port = int(os.environ.get("PORT", 8080))
uvicorn.run(app, host="0.0.0.0", port=port)
```
```python
# Python / Flask
app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```
```js
// Node / Express
const port = process.env.PORT || 8080;
app.listen(port, "0.0.0.0");
```
```go
port := os.Getenv("PORT"); if port == "" { port = "8080" }
http.ListenAndServe("0.0.0.0:"+port, nil)
```

---

## 1. Get gcloud

**Fastest path with zero install: use Cloud Shell.** Go to
`console.cloud.google.com`, click the `>_` icon in the top right. gcloud is already
installed *and already authenticated*. Push your code to GitHub, `git clone` it in
Cloud Shell, deploy from there.

**On the Mac:**
```
brew install --cask gcloud-cli
```
Takes ~3 min. Then **open a new terminal tab** — the PATH won't be set in the old one.

```
gcloud auth login
```

---

## 2. Set the project — use the ID, not the number

```
gcloud projects list
```

You'll see `PROJECT_ID` and `PROJECT_NUMBER`. **Use the ID.**

```
gcloud config set project YOUR-PROJECT-ID
```

If you set the number, `gcloud run deploy` fails with:
> `The value of ``core/project`` property is set to project number. To use this
> command, set --project to PROJECT ID`

If `gcloud projects list` comes back **empty**, your account isn't attached to a
project. That's an event-ops problem — go find a Google person, don't debug it.

---

## 3. Enable the APIs

Paste as **one line**:

```
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

Takes ~60s. Returns `Operation ... finished successfully.`

⚠️ **Watch for line wrapping.** If your terminal wraps a long command, pasting can
split it into two commands and you get `command not found: artifactregistry.googleapis.com`.
This bit us twice. If a command is long, put it in a script (step 6).

---

## 4. Deploy

From your project directory:

```
gcloud run deploy myapp --source . --region us-east1 --allow-unauthenticated --port 8080
```

It'll ask to create an Artifact Registry repo — say **y**. First build takes 3–4 min.

You get back:
> `Service URL: https://myapp-xxxxx.us-east1.run.app`

**That's the gate. You're eligible.**

### How `--source .` decides what to build
- **If you have a `Dockerfile`**, it uses it.
- **If you don't**, it uses buildpacks, which need:
  - Python → `requirements.txt` + a `Procfile` containing
    `web: uvicorn main:app --host 0.0.0.0 --port $PORT`
  - Node → `package.json` with a `start` script
  - Go → `go.mod`

A minimal Dockerfile if you want control:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port $PORT
```

---

## 5. Secrets — never bake them into the image

Put them in `env.yaml`:
```yaml
OPENAI_API_KEY: "sk-..."
GOOGLE_API_KEY: "..."
```

Add `env.yaml` to **both** `.gitignore` and `.dockerignore`, then:

```
gcloud run deploy myapp --source . --region us-east1 --allow-unauthenticated --env-vars-file env.yaml
```

They arrive as normal environment variables at runtime and never enter the image.

---

## 6. Put it in a script (do this — it prevents the wrap bug)

`deploy.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
gcloud run deploy myapp \
  --source . \
  --region us-east1 \
  --allow-unauthenticated \
  --port 8080 \
  --min-instances 1 \
  --no-cpu-throttling \
  --env-vars-file env.yaml

URL=$(gcloud run services describe myapp --region us-east1 --format='value(status.url)')
echo "Live: $URL"
curl -sS -o /dev/null -w "health: HTTP %{http_code}\n" "$URL/"
```

```
chmod +x deploy.sh && ./deploy.sh
```

Now redeploying is one short command that can't get split by a line wrap.

---

## 7. Two flags that matter for a live demo

| Flag | Why |
|---|---|
| `--min-instances 1` | Without it, Cloud Run scales to zero and the next visitor eats a **~10s cold start**. You don't want that happening in front of judges. |
| `--no-cpu-throttling` | By default CPU is only allocated *during a request*. Any background task — a polling loop, a cache refresher — silently freezes between requests. Cost us a stale cache we didn't notice until we checked. |

Others worth knowing: `--memory 512Mi`, `--timeout 300` (long requests),
`--concurrency 80`.

---

## 8. When it breaks

**Deployed but every request 404s / times out** → you're not binding `0.0.0.0:$PORT`.
This is 90% of failures.

**Build fails** → read the Cloud Build log; the deploy output prints a direct link.
Usually a missing dep in `requirements.txt` / `package.json`.

**403 Forbidden on your URL** → you forgot `--allow-unauthenticated`. Fix:
```
gcloud run services add-iam-policy-binding myapp --region us-east1 --member=allUsers --role=roles/run.invoker
```

**Works locally, breaks in the container** → something on your Mac that isn't in the
image. Watch for timezone data (`python:3.12-slim` ships no IANA tz database — add
`tzdata` to requirements) and for data files excluded by `.dockerignore`.

**See what's actually happening:**
```
gcloud run services logs read myapp --region us-east1 --limit 50
```

**Check what routes the container really registered** (FastAPI):
```
curl -s https://YOUR-URL/openapi.json | python3 -m json.tool | grep '"/'
```

---

## The 60-second version

```
brew install --cask gcloud-cli          # new terminal tab after
gcloud auth login
gcloud projects list                    # copy the PROJECT_ID
gcloud config set project PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
gcloud run deploy myapp --source . --region us-east1 --allow-unauthenticated --port 8080
```

Bind `0.0.0.0:$PORT`. Use the project **ID**. Deploy early, iterate after.

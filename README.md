# Typeform Exporter

FastAPI service to export Typeform responses and generate Google Sheet output.

## Deploy to Railway (auto deploy from GitHub)

1. Go to [Railway](https://railway.app) and create a new project.
2. Choose **Deploy from GitHub repo** and select this repository:
   - `https://github.com/BruceMIXXIN/typeform`
3. Railway will auto-detect and start deployment.
4. In Railway Variables, set:
   - `TYPEFORM_TOKEN`
   - `GOOGLE_CREDENTIALS` (service account JSON in one line)
   - `GDRIVE_FOLDER_ID`
   - `API_SECRET`
5. Redeploy once variables are saved.

## Health check

- `GET /health` should return `{ "status": "ok" }`

## Local run

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

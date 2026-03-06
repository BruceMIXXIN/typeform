#!/usr/bin/env python3"""
Typeform 問卷名單匯出工具 — Railway 版
POST /export       → 抓資料、清洗、寫入 Google Sheets，回傳結果 + 暫存 CSV
GET  /download?form_id=xxx  → 下載合併 CSV（email + name + phone）
"""

import os
import json
import math
import time
import tempfile
import requests
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─────────────────────────────────────────────
# 設定（Railway 環境變數）
# ─────────────────────────────────────────────
TYPEFORM_TOKEN     = os.environ["TYPEFORM_TOKEN"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
GDRIVE_FOLDER_ID   = os.environ["GDRIVE_FOLDER_ID"]
GDRIVE_DRIVE_ID    = os.environ.get("GDRIVE_DRIVE_ID", "")
API_SECRET         = os.environ.get("API_SECRET", "")
PORT               = int(os.environ.get("PORT", 8080))

PAGE_SIZE = 1000
MAX_PAGES = 30


app = FastAPI(title="Typeform Exporter")
bearer = HTTPBearer(auto_error=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────
def verify_token(credentials: HTTPAuthorizationCredentials = Security(bearer)):
    if not API_SECRET:
        return
    if not credentials or credentials.credentials != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────
class ExportRequest(BaseModel):
    form_id: str

class ExportResponse(BaseModel):
    form_id: str
    sheet_url: str
    email_count: int
    phone_count: int
    elapsed_seconds: float


# ─────────────────────────────────────────────
# Google 服務
# ─────────────────────────────────────────────
def get_google_services():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(creds_dict, f)
        tmp_path = f.name

    creds = Credentials.from_service_account_file(tmp_path, scopes=scopes)
    os.unlink(tmp_path)

    return build("sheets", "v4", credentials=creds), build("drive", "v3", credentials=creds)


# ─────────────────────────────────────────────
# Step 1：抓取 Typeform
# ─────────────────────────────────────────────
def fetch_all_responses(form_id: str) -> list:
    all_responses, before_cursor, page_num = [], None, 0

    while page_num < MAX_PAGES:
        page_num += 1
        params = {"page_size": PAGE_SIZE}
        if before_cursor:
            params["before"] = before_cursor

        resp = requests.get(
            f"https://api.typeform.com/forms/{form_id}/responses",
            headers={"Authorization": f"Bearer {TYPEFORM_TOKEN}"},
            params=params, timeout=30,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        all_responses.extend(items)
        print(f"   第 {page_num} 頁：{len(items)} 筆，累計 {len(all_responses)} 筆")

        if len(items) < PAGE_SIZE:
            break
        before_cursor = items[-1]["token"]

    return all_responses


# ─────────────────────────────────────────────
# Step 2：清洗資料
# ─────────────────────────────────────────────
def clean_responses(raw: list) -> list:
    seen_email, seen_phone, cleaned = set(), set(), []

    for entry in raw:
        answers = entry.get("answers", [])
        email = next((a.get("email", "") for a in answers if a.get("type") == "email"), "").lower()
        phone_raw = next((a.get("phone_number", "") for a in answers if a.get("type") == "phone_number"), "")
        phone = ("0" + phone_raw[4:]) if phone_raw.startswith("+886") else phone_raw

        if not email and not phone:
            continue

        is_email_dup = bool(email) and email in seen_email
        is_phone_dup = bool(phone) and phone in seen_phone
        if is_email_dup and is_phone_dup:
            continue

        if email: seen_email.add(email)
        if phone: seen_phone.add(phone)

        cleaned.append({
            "email": "" if is_email_dup else email,
            "phone": "" if is_phone_dup else phone,
        })

    return cleaned


# ─────────────────────────────────────────────
# Step 3：寫入 Google Sheets
# ─────────────────────────────────────────────
def create_and_write(sheets_svc, drive_svc, form_id: str, data: list):
    today = datetime.now().strftime("%m%d")
    title = f"TF_list_{form_id}_{today}"

    result = sheets_svc.spreadsheets().create(body={
        "properties": {"title": title},
        "sheets": [{"properties": {"title": "email"}}, {"properties": {"title": "phone"}}]
    }).execute()
    spreadsheet_id = result["spreadsheetId"]

    total = len(data)
    batch_size = 900 if total <= 2000 else (700 if total <= 4000 else 500)

    email_rows, phone_rows, serial = [], [], 1
    for row in data:
        if row["email"]:
            email_rows.append([row["email"], serial])
            serial += 1
        if row["phone"]:
            phone_rows.append([row["phone"]])

    # 寫入 email 分頁
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range="email!A1",
        valueInputOption="RAW", body={"values": [["email", "name"]]}
    ).execute()
    for i in range(0, len(email_rows), batch_size):
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=f"email!A{i+2}",
            valueInputOption="RAW", body={"values": email_rows[i:i+batch_size]}
        ).execute()
        if i + batch_size < len(email_rows): time.sleep(1)

    # 寫入 phone 分頁
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range="phone!A1",
        valueInputOption="RAW", body={"values": [["phone"]]}
    ).execute()
    for i in range(0, len(phone_rows), batch_size):
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=f"phone!A{i+2}",
            valueInputOption="RAW", body={"values": phone_rows[i:i+batch_size]}
        ).execute()
        if i + batch_size < len(phone_rows): time.sleep(1)

    # 移至目標資料夾
    file = drive_svc.files().get(fileId=spreadsheet_id, fields="parents", supportsAllDrives=True).execute()
    drive_svc.files().update(
        fileId=spreadsheet_id,
        addParents=GDRIVE_FOLDER_ID,
        removeParents=",".join(file.get("parents", [])),
        supportsAllDrives=True, fields="id, parents"
    ).execute()

    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    return sheet_url, email_rows, phone_rows




# ─────────────────────────────────────────────
# API 端點
# ─────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/export", response_model=ExportResponse)
def export(req: ExportRequest, _=Depends(verify_token)):
    form_id = req.form_id.strip()
    if not form_id:
        raise HTTPException(status_code=400, detail="form_id 不能為空")

    print(f"\n🚀 開始處理：{form_id}")
    start = time.time()

    try:
        raw     = fetch_all_responses(form_id)
        cleaned = clean_responses(raw)
        sheets_svc, drive_svc = get_google_services()
        sheet_url, email_rows, phone_rows = create_and_write(sheets_svc, drive_svc, form_id, cleaned)
    except requests.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Typeform API 錯誤：{e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    elapsed = round(time.time() - start, 1)

    # 合併 email/phone 為同一張表（依序對齊，不足補空）
    max_len = max(len(email_rows), len(phone_rows))
    merged_rows = []
    for i in range(max_len):
        email, name = email_rows[i] if i < len(email_rows) else ["", ""]
        phone       = phone_rows[i][0] if i < len(phone_rows) else ""
        merged_rows.append([email, name, phone])

    # 暫存供 CSV 下載
    _cache[form_id] = {
        "merged_rows": merged_rows,
        "timestamp": datetime.now().strftime("%m%d"),
    }

    print(f"🎉 完成！{elapsed}s → {sheet_url}")
    return ExportResponse(
        form_id=form_id,
        sheet_url=sheet_url,
        email_count=len(email_rows),
        phone_count=len(phone_rows),
        elapsed_seconds=elapsed,
    )


@app.get("/download")
def download_csv(form_id: str = Query(...), _=Depends(verify_token)):
    if form_id not in _cache:
        raise HTTPException(status_code=404, detail="找不到資料，請先執行匯出")
    cache = _cache[form_id]
    csv_bytes = make_csv(cache["merged_rows"], ["email", "name", "phone"])
    filename = f"TF_list_{form_id}_{cache['timestamp']}.csv"
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ─────────────────────────────────────────────
# 靜態前端（必須在所有 API 路由之後）
# ─────────────────────────────────────────────
static_dir = os.path.dirname(os.path.abspath(__file__))
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT)

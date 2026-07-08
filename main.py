from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, EmailStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional
import json
import sqlite3
import redis
import random
import smtplib
from email.mime.text import MIMEText

class Settings(BaseSettings):
    DB_NAME: str
    REDIS_HOST: str
    REDIS_PORT: int
    REDIS_DB: int
    
    SMTP_SERVER: str
    SMTP_PORT: int
    SMTP_USER: str
    SMTP_PASSWORD: str

    model_config = SettingsConfigDict(env_file=".env")

settings = Settings()

app = FastAPI(title="Grape API", description="노드를 연결하여 만든 프로그램(Grape) 및 이메일 인증 관리 API")

redis_client = redis.Redis(
    host=settings.REDIS_HOST, 
    port=settings.REDIS_PORT, 
    db=settings.REDIS_DB, 
    decode_responses=True
)

def init_db():
    with sqlite3.connect(settings.DB_NAME) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS grapes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root TEXT NOT NULL,
                raw TEXT NOT NULL
            )
        ''')
        conn.commit()

@app.on_event("startup")
def on_startup():
    init_db()
    try:
        redis_client.ping()
    except redis.ConnectionError:
        print("Redis 서버에 연결할 수 없습니다. Redis가 실행 중인지 확인하세요.")

def get_db():
    conn = sqlite3.connect(settings.DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

class Node(BaseModel):
    id: int
    inputs: List[str]
    outputs: List[str]
    next: Optional[int] = None

class Grape(BaseModel):
    id: int
    root: List[Node]
    raw: str

class StandardResponse(BaseModel):
    ok: bool
    message: str

class GrapeResponse(StandardResponse):
    grape: Optional[Grape] = None

class GrapesResponse(StandardResponse):
    grapes: List[Grape]

class EmailRequest(BaseModel):
    email: EmailStr

class VerifyRequest(BaseModel):
    email: EmailStr
    code: str

def send_verification_email(email_to: str, code: str):
    msg = MIMEText(f"인증 번호는 [{code}] 입니다.\n해당 인증 번호는 3분 동안 유효합니다.")
    msg['Subject'] = 'Grape API 이메일 인증 번호'
    msg['From'] = settings.SMTP_USER
    msg['To'] = email_to

    try:
        with smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"이메일 발송 실패: {e}")
        raise e

@app.post("/auth/send-code", response_model=StandardResponse)
def send_code(req: EmailRequest, background_tasks: BackgroundTasks):
    verification_code = str(random.randint(100000, 999999))
    
    redis_client.setex(name=req.email, time=180, value=verification_code)
    
    try:
        background_tasks.add_task(send_verification_email, req.email, verification_code)
        return {"ok": True, "message": "인증 번호가 이메일로 발송되었습니다. 3분 안에 입력해주세요."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="이메일 발송에 실패했습니다.")

@app.post("/auth/verify-code", response_model=StandardResponse)
def verify_code(req: VerifyRequest):
    stored_code = redis_client.get(req.email)
    
    if not stored_code:
        return {"ok": False, "message": "인증 번호가 만료되었거나 존재하지 않습니다."}
    
    if stored_code != req.code:
        return {"ok": False, "message": "잘못된 인증 번호입니다."}
    
    redis_client.delete(req.email)
    return {"ok": True, "message": "이메일 인증이 완료되었습니다."}

@app.post("/grapes", response_model=StandardResponse)
def create_grape(grape_in: List[Node], db: sqlite3.Connection = Depends(get_db)):
    raw_str = "ㅗ"
    
    root_json = json.dumps([node.model_dump() for node in grape_in])

    try:
        cursor = db.execute(
            "INSERT INTO grapes (root, raw) VALUES (?, ?)",
            (root_json, raw_str)
        )
        db.commit()
        new_id = cursor.lastrowid
        return {
            "ok": True,
            "message": f"Grape {new_id} saved successfully."
        }
    
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code = 500,
            detail = f"Database Error: {str(e)}"
        )


@app.get("/grapes/{grape_id}", response_model=GrapeResponse)
def get_grape(grape_id: int, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.execute("SELECT id, root, raw FROM grapes WHERE id = ?", (grape_id,))
    row = cursor.fetchone()
    
    if not row:
        return {
            "ok": False,
            "message": "Grape not found.",
            "grape": None}
    
    grape_data = {
        "id": row["id"],
        "root": json.loads(row["root"]),
        "raw": row["raw"]
    }
    return {
        "ok": True,
        "message": "Success",
        "grape": grape_data
    }
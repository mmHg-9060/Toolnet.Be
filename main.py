from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request, Response, Query
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional, NewType, Tuple
from datetime import datetime, timedelta
import jwt
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
    
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_MINUTES: int = 60 * 24

    model_config = SettingsConfigDict(env_file=".env")

settings = Settings()

app = FastAPI(title="Grape API", description="노드를 연결하여 만든 프로그램(Grape) 및 이메일/JWT 인증 관리 API")

redis_client = redis.Redis(
    host=settings.REDIS_HOST, 
    port=settings.REDIS_PORT, 
    db=settings.REDIS_DB, 
    decode_responses=True
)

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRATION_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def get_current_user(request: Request) -> str:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized: 인증 토큰이 필요합니다.")
    
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Unauthorized: 유효하지 않은 토큰입니다.")
        return email
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Unauthorized: 토큰이 만료되었습니다.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Unauthorized: 유효하지 않은 토큰입니다.")

def init_db():
    with sqlite3.connect(settings.DB_NAME) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS grapes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT,
                meta TEXT NOT NULL,
                nodes TEXT NOT NULL,
                raw TEXT NOT NULL,
                downloads INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

class GrapeMeta(BaseModel):
    name: str
    description: str

NodeId = NewType('NodeId', int)

class Node(BaseModel):
    id: NodeId
    name: str
    sans: List[Tuple[str, str]]
    next: Optional[int] = None

GrapeId = NewType('GrapeId', int)

class Grape(BaseModel):
    id: GrapeId
    meta: GrapeMeta
    nodes: List[Node]
    raw: str

class GrapeCreateRequest(BaseModel):
    meta: GrapeMeta
    nodes: List[Node]
    raw: str

class StandardResponse(BaseModel):
    ok: bool
    message: str

class GrapeResponse(StandardResponse):
    data: Optional[Grape] = None

class GrapesResponse(StandardResponse):
    data: List[Grape] = []

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
    except Exception:
        raise HTTPException(status_code=500, detail="이메일 발송에 실패했습니다.")

@app.post("/auth/verify-code", response_model=StandardResponse)
def verify_code(req: VerifyRequest, response: Response):
    stored_code = redis_client.get(req.email)
    
    if not stored_code:
        return {"ok": False, "message": "인증 번호가 만료되었거나 존재하지 않습니다."}
    
    if stored_code != req.code:
        return {"ok": False, "message": "잘못된 인증 번호입니다."}
    
    redis_client.delete(req.email)
    
    access_token = create_access_token(data={"sub": req.email})
    
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.JWT_EXPIRATION_MINUTES * 60,
        samesite="lax"
    )
    
    return {
        "ok": True, 
        "message": "이메일 인증이 완료되었습니다."
    }

PAGE_SIZE = 10

@app.post("/grapes", response_model=StandardResponse)
def create_grape(req: GrapeCreateRequest, db: sqlite3.Connection = Depends(get_db), current_user: str = Depends(get_current_user)):
    
    for node in req.nodes:
        for s in node.sans:
            if len(s) != 2:
                raise HTTPException(
                    status_code=422, 
                    detail=f"Unprocessable Entity: 노드 '{node.name}'의 sans 데이터는 반드시 2개의 원소를 가져야 합니다."
                )

    meta_json = req.meta.model_dump_json()
    nodes_json = json.dumps([node.model_dump() for node in req.nodes])

    try:
        cursor = db.execute(
            "INSERT INTO grapes (user, meta, nodes, raw) VALUES (?, ?, ?, ?)",
            (current_user, meta_json, nodes_json, req.raw)
        )
        db.commit()
        new_id = cursor.lastrowid
        return {
            "ok": True,
            "message": f"Grape {new_id} saved successfully by {current_user}." 
        }
    
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Database Error: {str(e)}"
        )

@app.get("/grapes/{grape_id}", response_model=GrapeResponse)
def get_grape(grape_id: int, db: sqlite3.Connection = Depends(get_db)):
    cursor = db.execute("SELECT id, meta, nodes, raw FROM grapes WHERE id = ?", (grape_id,))
    row = cursor.fetchone()
    
    if not row:
        return {
            "ok": False,
            "message": "Grape not found.",
            "data": None
        }
    
    grape_data = {
        "id": row["id"],
        "meta": json.loads(row["meta"]),
        "nodes": json.loads(row["nodes"]),
        "raw": row["raw"]
    }
    return {
        "ok": True,
        "message": "Success",
        "data": grape_data
    }

@app.get("/grapes", response_model=GrapesResponse)
def get_grapes_list(
    page: int = Query(..., description="몇번째 페이지 요청인가?"),
    user: Optional[str] = Query(None, description="어떤 유저의 노드에 대한 요청인가?"),
    sort: Optional[int] = Query(None, description="정렬 방식 (0:오래된순, 1:최신순, 2:다운로드 적은순, 3:다운로드 많은순)"),
    db: sqlite3.Connection = Depends(get_db)
):
    if page < 1:
        page = 1
        
    offset = (page - 1) * PAGE_SIZE
    
    query_parts = ["SELECT id, meta, nodes, raw FROM grapes"]
    conditions = []
    params = []
    
    if user:
        conditions.append("user = ?")
        params.append(user)
        
    if conditions:
        query_parts.append("WHERE " + " AND ".join(conditions))
        
    sort_mapping = {
        0: "id ASC",
        1: "id DESC",
        2: "downloads ASC",
        3: "downloads DESC"
    }
    
    order_by = sort_mapping.get(sort, "id DESC")
    query_parts.append(f"ORDER BY {order_by} LIMIT ? OFFSET ?")
    
    params.extend([PAGE_SIZE, offset])
    final_query = " ".join(query_parts)
    
    try:
        cursor = db.execute(final_query, tuple(params))
        rows = cursor.fetchall()
        
        grapes_list = []
        for row in rows:
            grapes_list.append({
                "id": row["id"],
                "meta": json.loads(row["meta"]),
                "nodes": json.loads(row["nodes"]),
                "raw": row["raw"]
            })
            
        return {
            "ok": True,
            "message": "Success",
            "data": grapes_list
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database Error: {str(e)}"
        )
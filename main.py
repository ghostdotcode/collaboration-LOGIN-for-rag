import json
import asyncio
import re
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel

# Import our newly created layers
from database import engine, get_db
from models import Base, User
from schemas import UserSignupSchema, UserLoginSchema, TokenResponse
from auth import hash_password, verify_password, create_access_token, get_current_user
from ingestion.retriever import RAGRetriever

# 1. Automatically create the database tables if they don't exist
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Meritech Enterprise API")

# Enable CORS so your UI developers can connect from their local servers (e.g., localhost:3000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize the RAG Retriever
retriever = RAGRetriever()

class QueryRequest(BaseModel):
    query: str


# ── AUTHENTICATION ENDPOINTS ──────────────────────────────────────────────────

@app.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: UserSignupSchema, db: Session = Depends(get_db)):
    # 1. Check if email is already taken
    existing_user = db.query(User).filter(User.email == payload.email_address).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    # 2. Create the user and hash the password
    new_user = User(
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email_address,
        password_hash=hash_password(payload.password)
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # 3. Generate a JWT token to instantly log them in
    token = create_access_token(data={"sub": new_user.email})
    return TokenResponse(
        access_token=token, 
        user_name=f"{new_user.first_name} {new_user.last_name}"
    )


@app.post("/login", response_model=TokenResponse)
def login(payload: UserLoginSchema, db: Session = Depends(get_db)):
    # 1. Find the user
    user = db.query(User).filter(User.email == payload.email_address).first()
    
    # 2. Verify existence and password
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # 3. Generate a JWT token
    token = create_access_token(data={"sub": user.email})
    return TokenResponse(
        access_token=token, 
        user_name=f"{user.first_name} {user.last_name}"
    )


# ── RAG CHAT ENDPOINT (Migrated to FastAPI) ───────────────────────────────────

@app.post("/api/ask")
async def ask_question(request: QueryRequest):
    async def event_stream():
        yield f"data: {json.dumps({'type': 'status', 'content': 'Searching knowledge base…'})}\n\n"

        try:
            # Run the synchronous retriever inside an async thread pool
            loop = asyncio.get_event_loop()
            full_response = await loop.run_in_executor(None, retriever.answer_question, request.query)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            return

        think_match = re.search(r"<think>(.*?)</think>", full_response, re.DOTALL)
        if think_match:
            thinking_raw = think_match.group(1).strip()
            answer_raw = full_response[think_match.end():].strip()
        else:
            thinking_raw = ""
            answer_raw = full_response.strip()

        if thinking_raw:
            for token in re.split(r'(\s+)', thinking_raw):
                if token:
                    yield f"data: {json.dumps({'type': 'thinking', 'content': token})}\n\n"
                    await asyncio.sleep(0.005)

        yield f"data: {json.dumps({'type': 'thinking_done'})}\n\n"

        for token in re.split(r'(\s+)', answer_raw):
            if token:
                yield f"data: {json.dumps({'type': 'answer', 'content': token})}\n\n"
                await asyncio.sleep(0.015)

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )
# Meritech Enterprise API

> **Start here:** run `scripts/run_all.sh`, open http://localhost:8000. Full description of the RAG redesign, measurements and edge-case testing: [`docs/RAG_UPGRADE.md`](docs/RAG_UPGRADE.md).

## Overview
Meritech Enterprise API is a backend system designed for Retrieval-Augmented Generation (RAG) capabilities with user authentication. It features a modern API architecture to handle chat queries against a knowledge base, alongside user onboarding (signup/login) flows.

The project currently has two entry points for the backend (Flask and FastAPI) as it appears to be undergoing a transition to a more robust, asynchronous FastAPI architecture. 

## Tech Stack
### Backend
- **Python 3**
- **FastAPI** (New main application, in `main.py`)
- **Flask** (Legacy application, in `app.py`)
- **SQLAlchemy** (ORM for database interactions)
- **Uvicorn** (ASGI server for FastAPI)

### AI / NLP / RAG Pipeline
- **LangChain** & **LangGraph** (Orchestration of language models and RAG workflows)
- **OpenAI / Groq / HuggingFace** (Model integrations)
- **Sentence Transformers** (Embeddings)
- **PyMuPDF4LLM** (PDF parsing and ingestion)
- **spaCy & fastcoref** (NLP and coreference resolution)

### Database
- **PostgreSQL** with **pgvector** extension (Vector database for storing and querying embeddings)
- **Docker & Docker Compose** (Containerization of the database layer and pgAdmin)

### Frontend
- **Vanilla HTML / CSS / JS** (Located in the `frontend/` directory)
- JWT-based authentication for securing endpoints.

## Current State & What's Working
1. **Database & Infrastructure**: Docker compose file is set up to spin up a PostgreSQL instance with `pgvector` and `pgAdmin`.
2. **Authentication**: `main.py` (FastAPI) has fully functional JWT-based `/login` and `/signup` endpoints connected to the Postgres database.
3. **RAG Backend Pipeline**: The `ingestion/` module has scripts to chunk, enrich, index, and retrieve data. The API endpoint `/api/ask` (FastAPI) handles streaming responses for RAG queries using Server-Sent Events (SSE).
4. **Frontend UI**: Basic Vanilla JS frontend for login and signup (`frontend/login.html`, `frontend/signup.html`).

## What's Missing / Next Steps
1. **Consolidate Backend**: Fully deprecate `app.py` (Flask) in favor of `main.py` (FastAPI) and clean up duplicate logic if any. 
2. **Frontend Wiring**: Ensure the frontend (`login.js` and `signup.js`) is correctly fetching from the `main.py` endpoints and properly storing the JWT tokens.
3. **Chat UI**: A frontend interface for the RAG chat (`/api/ask`) needs to be developed and integrated with the authentication token.
4. **Environment Variables**: Make sure the `.env.example` is comprehensive so developers know exactly what keys (e.g., OpenAI API Key, Database URI) are required.

## Getting Started

### 1. Start the Database
Start the PostgreSQL database with the pgvector extension using Docker Compose:
```bash
docker-compose up -d
```

### 2. Install Dependencies
Create a virtual environment and install the required Python packages:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Setup Environment Variables
Create a `.env` file in the root directory and populate it according to `.env.example`.

### 4. Run the API Server
Start the FastAPI application:
```bash
uvicorn main:app --reload --port 8000
```
*Note: The frontend static files are mounted at `/frontend`.*

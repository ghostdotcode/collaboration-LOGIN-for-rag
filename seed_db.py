import os
from dotenv import load_dotenv
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_postgres.vectorstores import PGVector
from langchain_core.documents import Document

# Load environment variables from your .env file
load_dotenv()

# Initialize the exact same Google Embedding model used in retriever.py
embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-2")

# Define the connection string for your running Docker Postgres instance
CONNECTION_STRING = os.getenv(
    "DATABASE_URL", 
    "postgresql+psycopg2://postgres:postgres@localhost:5432/meritech_db"
)

try:
    # Connect to the local pgvector database instance
    vector_store = PGVector(
        connection=CONNECTION_STRING,
        embeddings=embeddings,
        collection_name="enterprise_docs",
    )

    # Hardcoded document text representing what would normally be parsed from a PDF
    doc = Document(
        page_content="IT Hardware Policy: If an employee experiences a cracked screen on their company-issued laptop, they are entitled to an immediate replacement. The employee must file a High priority Helpdesk ticket detailing the damage.",
        metadata={"source": "IT_Policy_2026.pdf", "clearance_level": "public"}
    )

    print("Connecting to database and generating embeddings...")
    
    # Insert the document vectors into Postgres
    vector_store.add_documents([doc])
    
    print("Database successfully seeded! Your agent is ready to read.")

except Exception as e:
    print(f"Error seeding database: {str(e)}")
import json
from pathlib import Path
from typing import List
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import PGVector
from core.config import Config

class DatabaseIndexer:
    """
    Takes enriched chunks and converts them into dense vector embeddings,
    storing them inside the PostgreSQL (pgvector) database using Local Embeddings.
    """
    def __init__(self, db_url: str):
        print("[System] Initializing PGVector Indexer (Local HuggingFace Embeddings)...")
        
        if db_url.startswith("postgresql://"):
            self.db_url = db_url.replace("postgresql://", "postgresql+psycopg2://")
        else:
            self.db_url = db_url
            
        # 100% Free, Local, Unthrottled Embedding Model
        # 100% Free, Local, Enterprise-Grade Embedding Model
        self.embeddings = HuggingFaceEmbeddings(
            model_name="BAAI/bge-large-en-v1.5",
            model_kwargs={'device': 'cpu'} # Explicitly route to CPU to prevent GPU setup errors
        )
        self.collection_name = "meritech_policy"

    def index_documents(self, enriched_chunks_path: Path):
        print("[Indexer] Loading enriched chunks from JSON...")
        with open(enriched_chunks_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        documents: List[Document] = []
        for group in data:
            for child_text in group.get("enriched_child_chunks", []):
                
                metadata = group.get("metadata", {})
                metadata["parent_id"] = group.get("parent_id")
                metadata["parent_content"] = group.get("parent_content")
                
                doc = Document(page_content=child_text, metadata=metadata)
                documents.append(doc)

        print(f"[Indexer] Loaded {len(documents)} chunks. Running local vectorization at full speed...")

        # Blast everything into the database at once
        PGVector.from_documents(
            embedding=self.embeddings,
            documents=documents,
            collection_name=self.collection_name,
            connection_string=self.db_url,
            pre_delete_collection=True
        )
        
        print("[Indexer] Successfully indexed all documents into pgvector!")
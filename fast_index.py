import sys
from pathlib import Path

# Ensure the root directory is in the Python path
sys.path.append(str(Path(__file__).resolve().parent))

from core.config import Config
from ingestion.indexer import DatabaseIndexer

def run_fast_index():
    print("=== Starting Fast Database Injection ===")
    
    # Point directly to the JSON file we already successfully created
    final_path = Config.PROCESSED_DIR / "final_enriched_chunks.json"
    
    if not final_path.exists():
        print(f"[Error] Could not find {final_path}")
        return

    # 5. Database Indexing (Skipping steps 1-4)
    indexer = DatabaseIndexer(Config.DATABASE_URL)
    indexer.index_documents(final_path)
    
    print("=== Injection Complete ===")

if __name__ == "__main__":
    run_fast_index()
import sys
from pathlib import Path
import json

# Ensure the root directory is in the Python path
sys.path.append(str(Path(__file__).resolve().parent))

from core.config import Config
from ingestion.parser import LayoutAwareParser
from ingestion.coref import CorefProcessor
from ingestion.chunker import HierarchicalChunker
from ingestion.enricher import ContextEnricher
from ingestion.indexer import DatabaseIndexer

def run_ingestion_test():
    print("=== Starting Ingestion Pipeline Test ===")
    
    raw_file_name = "company_policy.pdf" 
    pdf_path = Config.RAW_DIR / raw_file_name
    output_path = Config.PROCESSED_DIR / "resolved_policy.md"

    if not pdf_path.exists():
        print(f"[Error] Please place your PDF at: {pdf_path}")
        return

    # 1. Parse the PDF
    parser = LayoutAwareParser()
    md_text = parser.parse_pdf_to_markdown(pdf_path)

    # 2. Resolve Coreferences (Pronouns)
    processor = CorefProcessor()
    resolved_text = processor.resolve_text(md_text)
    output_path.write_text(resolved_text, encoding="utf-8")

    # 3. Hierarchical Chunking
    chunker = HierarchicalChunker()
    hierarchical_chunks = chunker.chunk_document(resolved_text)
    
    # 4. Contextual Enrichment
    enricher = ContextEnricher()
    enriched_chunks = enricher.enrich_chunks(hierarchical_chunks)
    
    final_path = Config.PROCESSED_DIR / "final_enriched_chunks.json"
    final_path.write_text(json.dumps(enriched_chunks, indent=2), encoding="utf-8")
    print(f"[System] Final enriched pipeline payload saved to: {final_path}")
    
    # 5. Database Indexing
    indexer = DatabaseIndexer(Config.DATABASE_URL)
    indexer.index_documents(final_path)
    
    print("=== Test Complete ===")

if __name__ == "__main__":
    run_ingestion_test()
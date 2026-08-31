from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from typing import List, Dict, Any

class HierarchicalChunker:
    """
    Splits markdown into parent sections based on headers,
    then generates granular child chunks for precise vector retrieval.
    """
    def __init__(self):
        print("[System] Initializing Hierarchical Chunker...")
        # Define which headers we want to structure our parent chunks around
        self.headers_to_split_on = [
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
        ]
        self.markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=self.headers_to_split_on,
            strip_headers=False
        )
        
        # --- NEW: Parent Size Limiter ---
        # Forces massive markdown sections to break at ~4000 characters (approx 1,000 tokens) max
        self.parent_limiter = RecursiveCharacterTextSplitter(
            chunk_size=4000, 
            chunk_overlap=400
        )
        
        # Granular child chunker for precise vector search
        self.child_splitter = RecursiveCharacterTextSplitter(
            chunk_size=400,
            chunk_overlap=50,
        )

    def chunk_document(self, markdown_text: str) -> List[Dict[str, Any]]:
        print("[Chunker] Segmenting document into parent and child chunks...")
        
        # 1. Split into large parent chunks based on markdown structure
        raw_parent_docs = self.markdown_splitter.split_text(markdown_text)
        
        # 1.5 Enforce hard cap on parent chunk size
        # split_documents keeps the header metadata from the markdown splitter intact
        parent_docs = self.parent_limiter.split_documents(raw_parent_docs)
        
        hierarchical_chunks = []
        for i, parent in enumerate(parent_docs):
            # 2. Split each parent into smaller child chunks
            child_docs = self.child_splitter.split_text(parent.page_content)
            
            hierarchical_chunks.append({
                "parent_id": f"parent_{i}",
                "metadata": parent.metadata,
                "parent_content": parent.page_content,
                "child_chunks": child_docs
            })
            
        print(f"[Chunker] Successfully mapped {len(hierarchical_chunks)} safely-sized parent sections.")
        return hierarchical_chunks
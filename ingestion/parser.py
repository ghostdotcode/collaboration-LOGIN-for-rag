import pymupdf4llm
from pathlib import Path

class LayoutAwareParser:
    """
    Parses complex enterprise PDFs (like a 120-page manual) into structured Markdown,
    preserving tables, headers, and bounding box layouts.
    """
    def __init__(self):
        print("[System] Initializing Layout-Aware Parser...")

    def parse_pdf_to_markdown(self, file_path: Path) -> str:
        print(f"[Parser] Extracting structure from: {file_path.name}...")
        if not file_path.exists():
            raise FileNotFoundError(f"Could not find document at {file_path}")
        
        # Converts the PDF directly into Markdown format
        md_text = pymupdf4llm.to_markdown(str(file_path))
        print(f"[Parser] Successfully parsed {file_path.name}.")
        return md_text
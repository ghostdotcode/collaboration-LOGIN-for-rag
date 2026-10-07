"""On-demand page-image rendering for citation previews (cached on disk)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from core.config import Config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
CACHE_DIR = Config.DATA_DIR / "page_cache"


def render_page(doc_id: str, source_path: Optional[str], page: int, n_pages: Optional[int], dpi: int = 110) -> Optional[Path]:
    """
    Return a JPEG of one PDF page (1-based), rendering and caching on first use.
    Returns None when the page can't be produced: out of range, source missing,
    or not a PDF. `doc_id` is only ever a DB-validated, content-derived id, but
    the cache filename is still built from sanitised parts.
    """
    if not source_path or page < 1 or (n_pages and page > n_pages):
        return None
    src = Path(source_path)
    if src.suffix.lower() != ".pdf" or not src.is_file():
        return None

    safe_doc = "".join(c for c in doc_id if c.isalnum() or c in "-_")[:64]
    if not safe_doc:
        return None
    out = CACHE_DIR / safe_doc / f"{page}.jpg"
    if out.is_file():
        return out

    try:
        import pymupdf

        with _lock:  # MuPDF is not thread-safe
            if out.is_file():
                return out
            out.parent.mkdir(parents=True, exist_ok=True)
            with pymupdf.open(src) as pdf:
                if page > len(pdf):
                    return None
                pix = pdf[page - 1].get_pixmap(dpi=dpi)
                tmp = out.with_suffix(".tmp")
                tmp.write_bytes(pix.tobytes("jpeg", jpg_quality=82))
                tmp.replace(out)
        return out
    except Exception as exc:  # corrupt PDF, disk full, ...
        logger.warning("could not render page %s of %s: %s", page, doc_id, exc)
        return None

"""
Render a PDF's pages and embed each one with the visual model.

    .venv-visual/bin/python -m visual_service.index_pages data/raw/x.pdf --doc-id x

Resumable: pages that already have a vector file are skipped, so a CPU run that
takes a while can be interrupted and continued. The manifest is rewritten after
every page for the same reason.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

from visual_service import common

ROOT = Path(__file__).resolve().parents[1]


def render(page: "pymupdf.Page", dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--doc-id", required=True)
    ap.add_argument("--dpi", type=int, default=110)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "visual")
    ap.add_argument("--max-pages", type=int, default=0, help="0 = all pages")
    args = ap.parse_args()

    out = args.out / args.doc_id
    (out / "pages").mkdir(parents=True, exist_ok=True)
    (out / "vectors").mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"

    doc = pymupdf.open(args.pdf)
    total = len(doc) if not args.max_pages else min(args.max_pages, len(doc))
    manifest = {"doc_id": args.doc_id, "model": common.MODEL_NAME, "pages": []}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    done = {p["page"] for p in manifest["pages"]}

    common.load()
    started = time.perf_counter()
    for index in range(total):
        number = index + 1
        vec_file = out / "vectors" / f"p{number:04d}.npy"
        img_file = out / "pages" / f"p{number:04d}.jpg"
        if number in done and vec_file.exists() and img_file.exists():
            continue
        image = render(doc[index], args.dpi)
        image.save(img_file, "JPEG", quality=82)
        (vectors,) = common.embed_images([image])
        np.save(vec_file, vectors.astype(np.float16))
        manifest["pages"] = [p for p in manifest["pages"] if p["page"] != number]
        manifest["pages"].append(
            {"page": number, "vectors": f"vectors/{vec_file.name}", "image": f"pages/{img_file.name}",
             "n_patches": int(vectors.shape[0])}
        )
        manifest["pages"].sort(key=lambda p: p["page"])
        manifest_path.write_text(json.dumps(manifest, indent=1))
        elapsed = time.perf_counter() - started
        print(f"[{number}/{total}] {vectors.shape[0]} patches  ({elapsed / (index + 1):.1f}s/page avg)", flush=True)

    print(f"done: {len(manifest['pages'])} pages in {out}")


if __name__ == "__main__":
    main()

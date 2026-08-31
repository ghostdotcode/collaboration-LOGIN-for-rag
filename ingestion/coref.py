from fastcoref import FCoref
import spacy

class CorefProcessor:
    """
    Resolves dangling pronouns (it, they, this) to their actual noun entities.
    Processes text in batches to prevent Out-Of-Memory (OOM) errors on large documents.
    """
    def __init__(self, device: str = 'cpu'):
        print("[System] Loading Neural Coreference Model (this may take a moment)...")
        # Initialize the coref model (can be set to 'cuda' if you have a local GPU)
        self.model = FCoref(device=device)
        self.nlp = spacy.blank("en")
        self.nlp.add_pipe("sentencizer")

    def resolve_text(self, markdown_text: str) -> str:
        print("[Coref] Resolving entity references across document...")
        # We split by double-newline to process paragraph by paragraph.
        # Sending 120 pages into a transformer model at once will cause an OOM crash.
        paragraphs = markdown_text.split('\n\n')
        resolved_paragraphs = []

        for p in paragraphs:
            # Skip empty lines or very short structural headers.
            if len(p.strip()) < 15:
                resolved_paragraphs.append(p)
                continue
            
            try:
                # Predict and replace pronouns with their source nouns
                preds = self.model.predict(texts=[p])
                resolved_paragraphs.append(preds[0].get_resolved_text())
            except Exception as e:
                # Fallback: if a block fails (e.g., a massive code block/table), keep it as is
                resolved_paragraphs.append(p)
        
        print("[Coref] Coreference resolution complete.")
        return '\n\n'.join(resolved_paragraphs)
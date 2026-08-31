from typing import List, Dict, Any
from langchain_groq import ChatGroq
from langchain_core.prompts import PromptTemplate
from core.config import Config 

class ContextEnricher:
    """
    Enriches child chunks by prepending a fast LLM-generated summary 
    of their parent section's core entities and context.
    """
    def __init__(self):
        print("[System] Initializing Context Enricher (Groq Llama 3.1 8B)...")
        
        # Using Groq for rapid, cost-effective summarization
        self.llm = ChatGroq(
            model="openai/gpt-oss-20b", 
            temperature=0,
            groq_api_key=Config.GROQ_API_KEY 
        )
        
        self.prompt = PromptTemplate(
            input_variables=["document_context"],
            template=(
                "You are an expert document analyzer. Read the following section of a corporate policy.\n"
                "Write a concise, 1-sentence summary of what this section is about (maximum 15 words).\n"
                "IMPORTANT: Output ONLY the summary sentence. Do not include labels like 'Summary:', 'Entities:', or bullet points. "
                "If the text is blank or irrelevant, output 'General Policy Information'.\n\n"
                "Section Text:\n{document_context}\n\n"
                "Summary:"
            )
        )
        self.chain = self.prompt | self.llm

    def enrich_chunks(self, hierarchical_chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        print(f"[Enricher] Generating context headers for {len(hierarchical_chunks)} parent sections...")
        
        enriched_data = []
        for index, group in enumerate(hierarchical_chunks):
            parent_content = group["parent_content"]
            
            try:
                response = self.chain.invoke({"document_context": parent_content[:2000]})
                context_header = response.content.strip()
            except Exception as e:
                print(f"[Warning] LLM failed to generate context for parent {index}: {e}")
                context_header = "General Corporate Policy"
            
            print(f"  -> Generated Header {index+1}/{len(hierarchical_chunks)}: {context_header}")
            
            enriched_children = []
            for child in group["child_chunks"]:
                enriched_text = f"[Context: {context_header}]\n\n{child}"
                enriched_children.append(enriched_text)
                
            group["enriched_child_chunks"] = enriched_children
            enriched_data.append(group)
            
        print("[Enricher] Contextual enrichment complete.")
        return enriched_data
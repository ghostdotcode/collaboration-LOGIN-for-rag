import os
from typing import List
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import PGVector
from langchain_groq import ChatGroq
from langchain_core.prompts import PromptTemplate
from core.config import Config


class AdvancedRetriever:
    """
    Production RAG Retriever utilizing BGE-Large (1024-dim) local embeddings,
    Small-to-Big parent chunk synthesis, MMR search, and Llama 3.3 70B via Groq.
    """
    def __init__(self):
        print("[System] Initializing Advanced RAG Retriever...")
        
        # 1. Vector Store Connection (BGE-Large 1024-dim on CPU)
        self.embeddings = HuggingFaceEmbeddings(
            model_name="BAAI/bge-large-en-v1.5",
            model_kwargs={'device': 'cpu'}
        )
        
        # Ensure psycopg2 driver compatibility for LangChain PGVector
        db_url = Config.DATABASE_URL
        if db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+psycopg2://")
            
        self.vector_store = PGVector(
            collection_name="meritech_policy",
            connection_string=db_url,
            embedding_function=self.embeddings,
        )
        
# 2. Generative Engine (Reverted to Qwen, but with fixed token math!)
        self.llm = ChatGroq(
            model="qwen/qwen3.8-27b",
            groq_api_key=Config.GROQ_API_KEY,
            temperature=0.1,
            # Lowered from 8192. This leaves ~7000 tokens safely available for your prompt!
            max_tokens=1024  
        )
    def answer_question(self, query: str) -> str:
        print(f"\n[Retriever] Query: '{query}'")
        
        # TECHNIQUE 1: Maximal Marginal Relevance (MMR) Search
        # Fetches 10 candidates, selects top 2 most diverse child chunks
        child_docs = self.vector_store.max_marginal_relevance_search(query, k=2, fetch_k=10)
        
        # TECHNIQUE 2: Small-to-Big Retrieval (Parent Context Extraction)
        unique_parents = {}
        for doc in child_docs:
            parent_id = doc.metadata.get("parent_id", "unknown_id")
            if parent_id not in unique_parents:
                unique_parents[parent_id] = doc.metadata.get("parent_content", doc.page_content)
        
        synthesized_context = "\n\n---\n\n".join(unique_parents.values())
        
        # TECHNIQUE 3: Safe Token & Character Safety Valve
        # Caps context at ~18,000 characters (~4,500 tokens) to stay well under TPM rate limits
        MAX_CONTEXT_CHARS = 18000
        if len(synthesized_context) > MAX_CONTEXT_CHARS:
            print(f"[Retriever] ⚠ Context payload ({len(synthesized_context)} chars) exceeds safe ceiling. Truncating.")
            synthesized_context = synthesized_context[:MAX_CONTEXT_CHARS] + "\n\n...[Context Truncated for Length]..."

        print(f"[Retriever] Synthesized {len(unique_parents)} unique parent sections for LLM context.")

        # TECHNIQUE 4: Strict Grounding Prompt Template
        prompt = PromptTemplate(
            input_variables=["context", "question"],
            template=(
                "You are the official Meritech AI Assistant.\n"
                "You must answer the user's question using ONLY the provided policy context below.\n"
                "Before answering, you MUST think step-by-step. Wrap your reasoning inside <think> and </think> tags.\n"
                "After the </think> tag, provide your final answer. Use clean Markdown formatting, bullet points, and bold text to make your final answer easy to read.\n\n"
                "Context:\n{context}\n\n"
                "Question: {question}\n\n"
                "Answer:"
            )
        )
        
        # LCEL Chain Execution
        chain = prompt | self.llm
        print("[Retriever] Generating grounded response via Llama 3.3 70B...")
        
        response = chain.invoke({"context": synthesized_context, "question": query})
        return response.content


# Alias to guarantee backward compatibility with scripts expecting either name
RAGRetriever = AdvancedRetriever

import os
from typing import List
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langchain_postgres.vectorstores import PGVector
from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from core.config import Config

# Connect to the exact embedding model used during database seeding
embeddings = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-2",
    google_api_key=Config.GOOGLE_API_KEY
)

# Initialize generation model using an active production path
llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=Config.GOOGLE_API_KEY,
    temperature=0.1,
    max_output_tokens=4096
)

CONNECTION_STRING = Config.DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://")

class SearchInput(BaseModel):
    query: str = Field(description="The semantic search query compiled from the user's request.")
    user_role: str = Field(description="The security role of the active user (e.g., 'admin', 'employee', 'intern').")

@tool("search_enterprise_knowledgebase", args_schema=SearchInput)
def search_enterprise_knowledgebase(query: str, user_role: str) -> str:
    """
    Searches the internal corporate database for policy documentation using 
    Advanced MMR Diversity and Small-to-Big Parent Context retrieval.
    """
    try:
        vector_store = PGVector(
            connection=CONNECTION_STRING,
            embeddings=embeddings,
            collection_name="meritech_policy",
        )

        # RBAC Filtering
        allowed_clearances = ["public"]
        if user_role in ["employee", "admin"]:
            allowed_clearances.append("internal")
        if user_role == "admin":
            allowed_clearances.append("confidential")

        filter_dict = {"clearance_level": {"$in": allowed_clearances}}

        # ADVANCED TECHNIQUE: MMR Search for balance & diversity
        try:
            docs_with_scores = vector_store.max_marginal_relevance_search_with_score(
                query, k=3, fetch_k=15, filter=filter_dict
            )
        except Exception:
            docs_with_scores = vector_store.similarity_search_with_score(
                query, k=3, filter=filter_dict
            )

        relevant_chunks = []
        unique_parents = {}

        for doc, score in docs_with_scores:
            if score <= 1.0: 
                # ADVANCED TECHNIQUE: Small-to-Big Retrieval (Parent-Child Resolution)
                parent_id = doc.metadata.get("parent_id")
                parent_content = doc.metadata.get("parent_content")
                
                if parent_id and parent_content:
                    if parent_id not in unique_parents:
                        unique_parents[parent_id] = parent_content
                else:
                    source = doc.metadata.get('source', 'Company Policy')
                    relevant_chunks.append(f"[Source: {source}]: {doc.page_content}")

        for parent_id, content in unique_parents.items():
            relevant_chunks.append(f"[Comprehensive Parent Section {parent_id}]:\n{content}")

        if not relevant_chunks:
            return "Search complete. No highly confident or authorized documentation was found for this query."

        synthesized_context = "\n\n---\n\n".join(relevant_chunks)

        # Generate a direct, grounded answer using Gemini
        prompt = PromptTemplate(
            input_variables=["context", "question"],
            template=(
                "You are the official Meritech AI Assistant.\n"
                "Answer the user's question using ONLY the provided policy context below.\n"
                "If the answer is not in the context, reply: 'I cannot find the answer in the current policy.'\n\n"
                "Context:\n{context}\n\n"
                "Question: {question}\n\n"
                "Answer:"
            )
        )
        
        chain = prompt | llm
        response = chain.invoke({"context": synthesized_context, "question": query})
        return response.content

    except Exception as e:
        return f"Database Error: Unable to complete search due to: {str(e)}"
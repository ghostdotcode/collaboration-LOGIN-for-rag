import os
from datetime import datetime
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage

# Using Groq as free, fast LLM provider
from langchain_groq import ChatGroq

from tools.action_webhook import create_helpdesk_ticket
from tools.retriever import search_enterprise_knowledgebase
from core.state import AgentState

# Load environment variables from the .env file at runtime
load_dotenv()

# Verify API key configuration before compiling the network nodes
if not os.getenv("GROQ_API_KEY"):
    raise ValueError("Missing GROQ_API_KEY in environment configuration.")

# X-Factor 1: Deterministic Temperature
# In corporate workflows, we want accuracy and predictability, not creative hallucinations.
# Setting temperature=0 forces the model to choose the highest-probability tokens.
# Use Groq (free API, very fast, excellent tool calling support)
llm = ChatGroq(model="qwen/qwen3.6-27b", temperature=0, max_tokens=8192)

tools = [search_enterprise_knowledgebase, create_helpdesk_ticket]
llm_with_tools = llm.bind_tools(tools)

# X-Factor 3: Dynamic System Guardrails
def get_system_prompt(user_role: str) -> SystemMessage:
    """
    Generates a dynamic system prompt that grounds the AI in reality.
    It injects current temporal context so the agent knows what 'today' means.
    """
    current_time = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    
    prompt = f"""You are the Meritech Enterprise AI Assistant.
Your primary job is to assist {user_role}s by searching corporate knowledge and executing administrative tasks autonomously.

CURRENT CONTEXT:
- Time: {current_time}
- User Role: {user_role}

CRITICAL RULES:
1. NEVER guess company policies. Always use the 'search_enterprise_knowledgebase' tool first.
2. If the user asks to file a ticket, gather the necessary context from the knowledgebase, then use the 'create_helpdesk_ticket' tool.
3. If a tool fails, politely inform the user of the error and suggest a next step.
4. Maintain a highly professional, concise, and helpful corporate tone. Do not use emojis unless appropriate for a Slack webhook.
"""
    return SystemMessage(content=prompt)

# The main node function that LangGraph will call
def call_model(state: AgentState) -> dict:
    """
    This is the core reasoning node. It reads the conversation history, 
    injects the system rules, and asks the LLM what to do next.
    """
    # Defensive programming: using .get() prevents a KeyError runtime crash 
    # if the graph state channel desynchronizes from the compiled app schema.
    messages = state.get("messages", [])
    
    # In a real app, you'd extract the role from the active user's session token.
    # For this demo, we'll hardcode it as 'employee'.
    system_message = get_system_prompt(user_role="employee")
    
    # Prepend the system message to the conversation history
    full_conversation = [system_message] + list(messages)
    
    # Invoke the LLM. It will return either a text response OR a tool call.
    response = llm_with_tools.invoke(full_conversation)
    
    # We return a dictionary matching our AgentState keys.
    # Because of `add_messages` in our state.py, this is appended to the list, not overwritten.
    return {"messages": [response]}
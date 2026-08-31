import os
import requests
from pydantic import BaseModel, Field
from langchain_core.tools import tool

# X-Factor 1: Pydantic Schema for strict LLM input validation
class TicketPayload(BaseModel):
    summary: str = Field(description="A short, clear title for the IT ticket.")
    description: str = Field(description="Detailed explanation of the issue, request, or policy context.")
    priority: str = Field(description="Priority level: High, Medium, or Low", default="Medium")

# X-Factor 2: The Docstring is actually the LLM's system prompt for this tool
@tool("create_helpdesk_ticket", args_schema=TicketPayload)
def create_helpdesk_ticket(summary: str, description: str, priority: str) -> str:
    """
    Creates a new ticket in the corporate Helpdesk system (Jira/Slack).
    Use this tool ONLY when the user explicitly asks to file a ticket, report an issue, 
    or request hardware replacement based on company policy.
    """
    # In production, this comes from your .env file
    webhook_url = os.getenv("ACTION_WEBHOOK_URL", "https://your-test-webhook-url.com/post")

    payload = {
        "text": f"🚨 *New Helpdesk Ticket ({priority})*\n*Summary:* {summary}\n*Details:* {description}"
    }

    # X-Factor 3: Production-grade error handling and timeouts
    try:
        # Never leave a network call without a timeout in an agentic workflow
        response = requests.post(webhook_url, json=payload, timeout=5)
        response.raise_for_status() 
        return f"Success! Ticket created perfectly. Summary: '{summary}'"
    
    except requests.exceptions.Timeout:
        # We return errors as strings so the LLM can read them and apologize to the user
        return "Error: The ticketing system timed out. Please apologize to the user and ask them to try again later."
    except requests.exceptions.RequestException as e:
        return f"Error: Failed to create ticket. The system responded with: {str(e)}"
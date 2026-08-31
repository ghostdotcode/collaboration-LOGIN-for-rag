from typing import TypedDict, Annotated, Sequence
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    """
    The unified state schema for our agentic workflow.
    LangGraph relies on the Annotated channel to accumulate message history.
    """
    messages: Annotated[Sequence[BaseMessage], add_messages]
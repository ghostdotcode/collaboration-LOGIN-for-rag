from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

# Import our custom components
from core.state import AgentState
from agent.nodes import call_model, tools

# X-Factor 1: The Pre-built Tool Node
# LangGraph natively handles executing our Python functions and catching execution errors safely.
tool_node = ToolNode(tools)

# Initialize the State Machine with our minimalist AgentState
workflow = StateGraph(AgentState)

# 1. Define the Nodes (The "Stations" on our flowchart)
workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)

# 2. Define the Edges (How data moves between nodes)
# The graph always starts by calling the agent
workflow.add_edge(START, "agent")

# X-Factor 2: The Conditional Router
# After the agent thinks, it either outputs text to the user, OR it requests a tool call.
# `tools_condition` automatically inspects the agent's output. 
# If it sees a tool request, it routes to the "tools" node. If not, it routes to END.
workflow.add_conditional_edges(
    "agent",
    tools_condition,
    {"tools": "tools", END: END}
)

# 3. The Cyclic Loop
# Once the tools finish executing, we MUST route back to the agent so it can read 
# the tool's output (like the Jira ticket number) and formulate a final answer.
workflow.add_edge("tools", "agent")

# X-Factor 3: The Checkpointer (Persistence)
# For this MVP, we use an in-memory SQLite saver. In production, this becomes a PostgresSaver.
# This is what allows our agent to remember the conversation across multiple interactions.
memory = MemorySaver()

# Compile the graph into a runnable application
app = workflow.compile(checkpointer=memory)
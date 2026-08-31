from agent.graph import app
from langchain_core.messages import HumanMessage
from dotenv import load_dotenv

load_dotenv() # Loads your OpenAI API key and Webhook URL

# The thread_id is how the checkpointer remembers THIS specific conversation
config = {"configurable": {"thread_id": "meritech_demo_1"}}

print("Starting Meritech Agentic Demo...\n")

# Simulate the user asking a complex question
user_input = "My laptop screen has a crack and i can't see anything. Please file a high priority helpdesk ticket for a replacement right now. My location is the NY office."
initial_state = {"messages": [HumanMessage(content=user_input)]}

# Run the graph! 
for chunk in app.stream(initial_state, config=config, stream_mode="values"):
    last_message = chunk["messages"][-1]
    
    # We print the type of message so you can watch the agent "think" and "act" in real-time
    if last_message.type == "human":
        print(f"User: {last_message.content}")
    elif last_message.type == "ai" and not last_message.tool_calls:
        print(f"AI: {last_message.content}")
    elif last_message.type == "tool":
        print(f"Tool Result ({last_message.name}): {last_message.content[:100]}...")
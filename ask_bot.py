import sys
import io
from pathlib import Path

# Force UTF-8 output so Unicode characters (e.g. emojis from LLM) don't crash on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Ensure the root directory is in the Python path
sys.path.append(str(Path(__file__).resolve().parent))

from ingestion.retriever import AdvancedRetriever

def run_query():
    retriever = AdvancedRetriever()
    
    print("\n=============================================")
    print("Welcome to the Meritech HR Policy Bot Tester!")
    print("Type 'exit' to quit.")
    print("=============================================\n")
    
    while True:
        user_input = input("\nAsk a question about the policy: ")
        if user_input.lower() in ['exit', 'quit']:
            break
            
        if not user_input.strip():
            continue
            
        answer = retriever.answer_question(user_input)
        
        print("\n---------------- BOT ANSWER ----------------")
        print(answer)
        print("--------------------------------------------")

if __name__ == "__main__":
    run_query()
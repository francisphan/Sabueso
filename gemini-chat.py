import os
import sys
from google import genai
from dotenv import load_dotenv  # <-- 1. Import the library

def main():
    # 2. Load the environment variables from the .env file
    load_dotenv() 

    # Verify the API key is present
    if "GEMINI_API_KEY" not in os.environ:
        print("Error: GEMINI_API_KEY not found. Please add it to your .env file.")
        sys.exit(1)

    # Initialize the client (it automatically finds the key in os.environ)
    client = genai.Client()
    model_id = "gemini-2.5-flash" 

    print("=========================================")
    print(f" Gemini CLI Chat initialized ({model_id})")
    print(" Type 'exit' or 'quit' to end the chat.")
    print("=========================================\n")

    try:
        chat = client.chats.create(model=model_id)
        
        while True:
            user_input = input("You: ")
            
            if user_input.strip().lower() in ['quit', 'exit']:
                print("Goodbye!")
                break
                
            if not user_input.strip():
                continue
                
            response = chat.send_message(user_input)
            print(f"\nGemini: {response.text}\n")
            
    except KeyboardInterrupt:
        print("\nGoodbye!")
    except Exception as e:
        print(f"\nAn error occurred: {e}")

if __name__ == "__main__":
    main()

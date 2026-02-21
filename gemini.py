import os
import sys
from google import genai

def main():
    # 1. Verify the API key is present
    if "GEMINI_API_KEY" not in os.environ:
        print("Error: Please set the GEMINI_API_KEY environment variable.")
        sys.exit(1)

    # 2. Initialize the client
    client = genai.Client()
    
    # We are using the Flash model as it is optimized for fast, conversational text
    model_id = "gemini-2.5-flash" 

    print("=========================================")
    print(f" Gemini CLI Chat initialized ({model_id})")
    print(" Type 'exit' or 'quit' to end the chat.")
    print("=========================================\n")

    try:
        # 3. Start the chat session (this automatically tracks conversation history)
        chat = client.chats.create(model=model_id)
        
        # 4. Create the chat loop
        while True:
            user_input = input("You: ")
            
            # Handle exit commands
            if user_input.strip().lower() in ['quit', 'exit']:
                print("Goodbye!")
                break
                
            # Ignore empty inputs
            if not user_input.strip():
                continue
                
            # Send the message and print the response
            response = chat.send_message(user_input)
            print(f"\nGemini: {response.text}\n")
            
    except KeyboardInterrupt:
        print("\nGoodbye!")
    except Exception as e:
        print(f"\nAn error occurred: {e}")

if __name__ == "__main__":
    main()

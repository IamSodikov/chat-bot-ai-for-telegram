import configparser
from pyrogram import Client, filters
from pyrogram.types import Message, Contact
import asyncio
import os
import openai
from openai import OpenAIError
import re

# Read configuration from config.ini
config = configparser.ConfigParser()
config.read('config.ini')

# Get values from the config file
API_ID = config.get('telegram', 'api_id')
API_HASH = config.get('telegram', 'api_hash')
OPENAI_API_KEY = config.get('openai', 'api_key')
ADMIN_USERNAME = config.get('admin', 'username')  # Get admin username

# Get ignored users from config and convert them to a set of integers
ignored_users = set(map(int, config.get('ignored_users', 'user_ids').split(',')))

# Initialize OpenAI API
openai.api_key = OPENAI_API_KEY

# Dictionary to store active clients by session name
active_clients = {}
user_conversations = {}  # Dictionary to store conversation history
user_order_info = {}  # Dictionary to store user order information
pending_followups = {}  # Dictionary to track pending follow-up tasks
inactive_chats = set()  # Set to track chats where the bot is stopped

# Function to add messages to the conversation history
def add_to_conversation_history(user_id, role, content):
    if user_id not in user_conversations:
        user_conversations[user_id] = []
    user_conversations[user_id].append({"role": role, "content": content})
    # Limit conversation history to the last 20 messages
    if len(user_conversations[user_id]) > 20:
        user_conversations[user_id] = user_conversations[user_id][-20:]

# Function to read a file from the 'prompt_file' folder
def read_file(file_name):
    try:
        current_dir = os.path.dirname(__file__)
        file_path = os.path.join(current_dir, 'prompt_file', file_name)
        if not os.path.exists(file_path):
            return None
        with open(file_path, 'r', encoding='utf-8') as file:
            return file.read().strip()
    except Exception as e:
        print(f"Error while reading {file_name}: {str(e)}")
        return None

# Function to call OpenAI's GPT-4 or GPT-3.5 model with retry mechanism and detailed error logging
async def get_openai_response(user_id, user_message):
    # Ensure OpenAI does not respond to ignored users
    if user_id in ignored_users:
        print(f"OpenAI response blocked for ignored user {user_id}.")
        return None  # Do not generate a response for ignored users

    try:
        if "use gpt-4" in user_message.lower():
            model = "gpt-4"
            user_message = user_message.replace("use gpt-4", "").strip()
        else:
            model = "gpt-3.5-turbo"

        system_prompt = read_file('prompt.txt')
        if not system_prompt:
            return "Error: System prompt not found."

        messages = [{"role": "system", "content": system_prompt}]
        if user_id in user_conversations:
            messages.extend(user_conversations[user_id])
        messages.append({"role": "user", "content": user_message})

        # Retry mechanism
        for attempt in range(3):
            try:
                response = openai.ChatCompletion.create(
                    model=model,
                    messages=messages,
                    max_tokens=512,
                    temperature=0.7
                )
                gpt_response = response['choices'][0]['message']['content'].strip()
                add_to_conversation_history(user_id, "assistant", gpt_response)
                return gpt_response
            except OpenAIError as e:
                print(f"Attempt {attempt + 1}: OpenAI API error - {str(e)}")
                await asyncio.sleep(1)  # Wait and retry
            except Exception as e:
                print(f"Attempt {attempt + 1}: Unexpected error - {str(e)}")
                await asyncio.sleep(1)  # Wait and retry

        return "I'm having trouble accessing the AI at the moment. Please try again later."
    except Exception as e:
        error_message = f"Critical error in OpenAI response function: {str(e)}"
        print(error_message)
        return error_message

# Function to schedule a follow-up message after 10 minutes
async def schedule_follow_up(client, user_id):
    await asyncio.sleep(5)  # Wait for 10 minutes (600 seconds)

    # Check if the user is still active before sending a follow-up
    if user_id in inactive_chats:
        print(f"Follow-up for user {user_id} canceled due to /stop command.")
        return  # Exit the function if the chat is inactive

    if user_conversations.get(user_id) and user_conversations[user_id][-1]['role'] == 'assistant':
        follow_up_message = (
            "Qo'shimcha savollaringiz bormi?\n\nУ вас есть дополнительные вопросы?"
        )
        await client.send_message(user_id, follow_up_message)
        del pending_followups[user_id]  # Remove follow-up task from tracking

# Function to check if a phone number is provided and send to admin
async def check_and_send_to_admin(client, user_id):
    if user_id in user_order_info:
        user_info = user_order_info[user_id]
        if user_info.get("phone_number"):
            # Send phone number to the admin
            phone_number = user_info["phone_number"]

            # Send details as a message
            info_message = (
                f"User @{user_info.get('username', 'N/A')} has provided their phone number:\n"
                f"User ID: {user_id}\n"
                f"Phone Number: {phone_number}"
            )
            await client.send_message(ADMIN_USERNAME, info_message)

            # Notify the user
            await client.send_message(user_id, "Sizning telefon raqamingiz qabul qilindi va administratorga yuborildi 😊.\n\nВаш номер телефона был получен и отправлен администратору 😊.")

# Function to handle phone number
async def handle_phone_number(client, message, user_id):
    phone_number = None
    if isinstance(message, Contact):
        phone_number = message.contact.phone_number
    else:
        if re.fullmatch(r"(\+?\d{9,15})", message.text):
            phone_number = message.text

    if phone_number:
        if user_id not in user_order_info:
            user_order_info[user_id] = {"phone_number": None, "username": message.from_user.username}
        user_order_info[user_id]["phone_number"] = phone_number

        await message.reply_text("Telefon raqami qabul qilindi 😊.\n\nНомер телефона получен 😊.")
        await check_and_send_to_admin(client, user_id)

# Main function to start a new client session
async def start_new_client(session_name, initial=True):
    if session_name in active_clients:
        print(f"Client {session_name} is already active.")
        return active_clients[session_name]

    client = Client(session_name, api_id=API_ID, api_hash=API_HASH)

    @client.on_message(filters.private)
    async def respond_to_private_message(client, message):
        user_id = message.from_user.id

        # Ignore messages from specific users
        if user_id in ignored_users:
            print(f"Message from ignored user {user_id} received and ignored.")
            return  # Do not respond to ignored users

        # Handle /start command
        if message.text == "/start":
            if user_id in inactive_chats:
                inactive_chats.discard(user_id)  # Remove user from the set of inactive chats
                print(f"Bot reactivated for user {user_id}. All functions are now enabled.")
            return

        # Handle /stop command
        if message.text == "/stop":
            inactive_chats.add(user_id)  # Add user to the set of inactive chats
            print(f"Bot deactivated for user {user_id}. No further responses will be sent until reactivated.")

            # Cancel any pending follow-up task
            if user_id in pending_followups:
                pending_followups[user_id].cancel()
                del pending_followups[user_id]

            return

        # If the user has sent /stop, ignore further messages
        if user_id in inactive_chats:
            return  # Do not respond if /stop has been sent

        # Process incoming messages if the chat is active
        if user_id in pending_followups:
            pending_followups[user_id].cancel()
            del pending_followups[user_id]

        if message.contact or (message.text and re.fullmatch(r"(\+?\d{9,15})", message.text)):
            await handle_phone_number(client, message, user_id)
        else:
            user_text = message.text
            if user_text:
                add_to_conversation_history(user_id, "user", user_text)
                response = await get_openai_response(user_id, user_text)
                if response:  # Ensure response is not empty or from ignored users
                    await message.reply_text(response)

                # Schedule a follow-up task to check for user response after 10 minutes
                pending_followups[user_id] = asyncio.create_task(schedule_follow_up(client, user_id))

    try:
        await client.start()
        active_clients[session_name] = client
        print(f"Client {session_name} started successfully!")
    except Exception as e:
        print(f"Failed to start client {session_name}: {e}")

# Load all existing sessions at the start
async def load_all_existing_sessions():
    session_files = [f.split(".session")[0] for f in os.listdir() if f.endswith(".session")]
    successfully_started_sessions = []

    if session_files:
        for index, session_name in enumerate(session_files, start=1):
            try:
                await start_new_client(session_name, initial=False)
                successfully_started_sessions.append(session_name)
            except Exception as e:
                print(f"Failed to start existing client {session_name}: {e}")
    else:
        print("No existing sessions found.")

    return successfully_started_sessions

# Handle user input for adding new sessions
async def handle_user_input():
    while True:
        command = (await asyncio.to_thread(input, "Enter 'add' to create a new session, or 'exit' to stop: ")).strip().lower()
        if command == 'add':
            session_name = (await asyncio.to_thread(input, "Enter a session name for the new user (e.g., 'user1'): ")).strip()
            await start_new_client(session_name)
        elif command == 'exit':
            break
        else:
            print("Invalid command. Use 'add' to add a new session or 'exit' to stop.")
        await asyncio.sleep(0.1)  # Allow other coroutines to run

# Main entry function
async def main():
    # Load all existing sessions at the start
    await load_all_existing_sessions()

    # Start the user input handler in a separate task
    input_task = asyncio.create_task(handle_user_input())

    # Keep the script running to process incoming messages and handle user input
    await input_task

# Stop all clients
async def stop_all_clients():
    for session_name, client in active_clients.items():
        await client.stop()
        print(f"Client {session_name} stopped.")
    active_clients.clear()

# Run the main function in an asyncio event loop
try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nStopping all clients...")
    asyncio.run(stop_all_clients())

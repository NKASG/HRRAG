from flask import Flask, request, jsonify
import requests
import os
from dotenv import load_dotenv
from rag_backend import hr_bot
from datetime import datetime
import re
from usage_tracker import track_usage

load_dotenv()

app = Flask(__name__)

# ======================
# CONFIG
# ======================
VERIFY_TOKEN = os.getenv("MY_SECRET_VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# ======================
# DUPLICATE PROTECTION
# ======================
processed_messages = set()

# ======================
# WEBHOOK VERIFICATION
# ======================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook Verified ✅")
        return challenge, 200

    return "Verification failed", 403


# ======================
# RECEIVE WHATSAPP MESSAGE
# ======================
@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    print("Incoming:", data)

    try:
        value = data["entry"][0]["changes"][0]["value"]
        
        if 'statuses' in value:
            return jsonify(status="ignored"), 200

        if "messages" not in value:
            return jsonify(status="ignored"), 200

        message = value["messages"][0]
        message_id = message["id"]
        from_number = message["from"]
        user_text = message["text"]["body"]

        # ======================
        # DUPLICATE CHECK
        # ======================
        if message_id in processed_messages:
            print("Duplicate message ignored ✅")
            return jsonify(status="duplicate"), 200

        processed_messages.add(message_id)

        print(f"User: {from_number}")
        print(f"Message: {user_text}")

        # ==================
        # GREETING SYSTEM
        # ==================
        clean_text = user_text.lower().strip()

        name_match = re.search(r"(i am|i'm|my name is|this is)\s+([A-Za-z]+)", clean_text)
        user_name = name_match.group(2).capitalize() if name_match else None

        hour = datetime.now().hour
        time_greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"

        greetings = ["hi", "hello", "hey", "good morning", "good afternoon", "good evening"]

        # ==================
        # DECISION LOGIC
        # ==================
        if any(clean_text.startswith(greet) for greet in greetings):
            answer = (
                f"{time_greeting} 👋\n\n"
                f"I’m Orange‑HR, your HR AI Assistant{', ' + user_name if user_name else ''}.\n"
                "I can assist with leave, salary policy, benefits and onboarding.\n\n"
                "How can I help you today?"
            )

        elif any(word in clean_text for word in ["bye", "goodbye", "thanks", "thank you", "ok", "okay"]):
            answer = "You're welcome 😊 Let me know if you need any help."

        elif len(clean_text) <= 2:
            answer = "Hi 👋 Please type a full question so I can assist you."

        else:
            response = hr_bot.query(user_text)
            answer = response.get("raw_answer") or response.get("answer")

        # ==================
        # SEND RESPONSE
        # ==================
        send_whatsapp_message(from_number, answer)

        # ==================
        # USAGE TRACKING
        # ==================
        try:
            tokens_used = len(user_text.split()) + len(answer.split())
            cost_estimate = round(tokens_used * 0.00001, 5)

            track_usage(
                prompt=user_text,
                engine="llama-3.1-8b-instant",
                tokens_used=tokens_used,
                cost=cost_estimate
            )
        except Exception as e:
            print("⚠️ Usage tracking failed:", e)

    except Exception as e:
        print("⚠️ Error:", e)

    # Always return success to stop WhatsApp retries
    return jsonify(status="success"), 200


# ======================
# SEND MESSAGE TO USER
# ======================
def send_whatsapp_message(to_number, text_body):
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text_body},
    }

    response = requests.post(url, json=payload, headers=headers)

    if response.status_code != 200:
        print("Send Failed:", response.text)


# ======================
# RUN APP
# ======================
if __name__ == "__main__":
    app.run(port=5000, debug=True)

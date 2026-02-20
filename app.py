from flask import Flask, request, jsonify, render_template_string
import requests
import os
from dotenv import load_dotenv
from rag_backend import hr_bot
from datetime import datetime
import re
from usage_tracker import track_usage

load_dotenv()

app = Flask(__name__)

WEB_CHAT_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Orange-HR Web View</title>
  <style>
    body {
      font-family: Arial, sans-serif;
      background: #f5f7fb;
      margin: 0;
      padding: 0;
    }
    .container {
      max-width: 760px;
      margin: 32px auto;
      background: white;
      border-radius: 12px;
      box-shadow: 0 12px 30px rgba(0,0,0,0.08);
      overflow: hidden;
    }
    .header {
      background: #f97316;
      color: #fff;
      padding: 18px 22px;
      font-weight: 700;
      font-size: 18px;
    }
    #chat {
      height: 420px;
      overflow-y: auto;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      background: #fafafa;
    }
    .bubble {
      max-width: 78%;
      padding: 10px 14px;
      border-radius: 12px;
      white-space: pre-wrap;
      line-height: 1.35;
    }
    .user {
      align-self: flex-end;
      background: #dbeafe;
    }
    .bot {
      align-self: flex-start;
      background: #ffedd5;
    }
    .input-area {
      display: flex;
      gap: 10px;
      padding: 16px;
      border-top: 1px solid #eee;
      background: #fff;
    }
    #message {
      flex: 1;
      border: 1px solid #d1d5db;
      border-radius: 8px;
      padding: 10px;
      font-size: 14px;
    }
    button {
      border: none;
      background: #f97316;
      color: #fff;
      border-radius: 8px;
      padding: 10px 16px;
      font-weight: 600;
      cursor: pointer;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">Orange-HR Assistant (Web View)</div>
    <div id="chat">
      <div class="bubble bot">Hello 👋 Ask any HR-related question here.</div>
    </div>
    <form class="input-area" id="chatForm">
      <input id="message" type="text" placeholder="Type your HR question..." required />
      <button type="submit">Send</button>
    </form>
  </div>

  <script>
    const chat = document.getElementById('chat');
    const form = document.getElementById('chatForm');
    const messageInput = document.getElementById('message');

    function appendMessage(text, cls) {
      const div = document.createElement('div');
      div.className = `bubble ${cls}`;
      div.textContent = text;
      chat.appendChild(div);
      chat.scrollTop = chat.scrollHeight;
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const message = messageInput.value.trim();
      if (!message) return;

      appendMessage(message, 'user');
      messageInput.value = '';

      try {
        const res = await fetch('/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message })
        });

        const data = await res.json();
        appendMessage(data.answer || 'Sorry, no response available.', 'bot');
      } catch (err) {
        appendMessage('⚠️ Unable to reach Orange-HR right now.', 'bot');
      }
    });
  </script>
</body>
</html>
"""

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


def generate_answer(user_text: str) -> str:
    clean_text = user_text.lower().strip()

    name_match = re.search(r"(i am|i'm|my name is|this is)\s+([A-Za-z]+)", clean_text)
    user_name = name_match.group(2).capitalize() if name_match else None

    hour = datetime.now().hour
    time_greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"

    greetings = ["hi", "hello", "hey", "good morning", "good afternoon", "good evening"]

    if any(clean_text.startswith(greet) for greet in greetings):
        return (
            f"{time_greeting} 👋\n\n"
            f"I’m Orange‑HR, your HR AI Assistant{', ' + user_name if user_name else ''}.\n"
            "I can assist with leave, salary policy, benefits and onboarding.\n\n"
            "How can I help you today?"
        )

    if any(word in clean_text for word in ["bye", "goodbye", "thanks", "thank you", "ok", "okay"]):
        return "You're welcome 😊 Let me know if you need any help."

    if len(clean_text) <= 2:
        return "Hi 👋 Please type a full question so I can assist you."

    response = hr_bot.query(user_text)
    return response.get("raw_answer") or response.get("answer")


@app.route("/", methods=["GET"])
def web_view():
    return render_template_string(WEB_CHAT_TEMPLATE)


@app.route("/chat", methods=["POST"])
def web_chat():
    payload = request.get_json(silent=True) or {}
    user_text = (payload.get("message") or "").strip()

    if not user_text:
        return jsonify(answer="Please type a message."), 400

    answer = generate_answer(user_text)
    return jsonify(answer=answer), 200

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
        answer = generate_answer(user_text)

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

import os

from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

from google import genai


app = Flask(__name__)

# 環境変数
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# LINE
configuration = Configuration(
    access_token=LINE_CHANNEL_ACCESS_TOKEN
)

handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Gemini
client = genai.Client(api_key=GEMINI_API_KEY)


@app.route("/")
def home():
    return "LINE Gemini Bot is running!"


@app.route("/callback", methods=["POST"])
def callback():

    signature = request.headers.get("X-Line-Signature")

    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)

    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):

    user_message = event.message.text

    try:

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_message,
        )

        ai_text = response.text

    except Exception as e:
    app.logger.exception("Gemini API error: %s", e)
    ai_text = "申し訳ありません。AIでエラーが発生しました。"

    with ApiClient(configuration) as api_client:

        line_bot_api = MessagingApi(api_client)

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text=ai_text
                    )
                ]
            )
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

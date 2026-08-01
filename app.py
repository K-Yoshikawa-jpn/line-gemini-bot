import os
import threading

from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

from google import genai


app = Flask(__name__)

# =========================================================
# 環境変数
# =========================================================

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]


# =========================================================
# LINE
# =========================================================

configuration = Configuration(
    access_token=LINE_CHANNEL_ACCESS_TOKEN
)

handler = WebhookHandler(LINE_CHANNEL_SECRET)


# =========================================================
# Gemini
# =========================================================

client = genai.Client(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-3.6-flash"

# LINEユーザーごとのGeminiチャットセッション
chat_sessions = {}

# 同時アクセスによる辞書の競合を防ぐ
chat_sessions_lock = threading.Lock()


# =========================================================
# LINEの送信元を識別する
# =========================================================

def get_session_id(event):
    """
    LINEの送信元から、会話を識別するためのIDを作成する。

    1対1トーク:
        user:{user_id}

    グループ:
        group:{group_id}:user:{user_id}

    複数人トーク:
        room:{room_id}:user:{user_id}
    """

    source = event.source

    source_type = getattr(source, "type", None)
    user_id = getattr(source, "user_id", None)
    group_id = getattr(source, "group_id", None)
    room_id = getattr(source, "room_id", None)

    if source_type == "group" and group_id:
        # グループ内でもユーザーごとに会話を分ける
        return f"group:{group_id}:user:{user_id or 'unknown'}"

    if source_type == "room" and room_id:
        # 複数人トーク内でもユーザーごとに会話を分ける
        return f"room:{room_id}:user:{user_id or 'unknown'}"

    if user_id:
        return f"user:{user_id}"

    # 通常はここには来ないが、安全のための予備ID
    return "unknown"


def get_or_create_chat(session_id):
    """
    セッションIDに対応するGeminiチャットを取得する。
    なければ新しく作成する。
    """

    with chat_sessions_lock:

        if session_id not in chat_sessions:
            chat_sessions[session_id] = client.chats.create(
                model=MODEL_NAME,
            )

        return chat_sessions[session_id]


def reset_chat(session_id):
    """
    指定されたユーザーの会話履歴を削除する。
    """

    with chat_sessions_lock:
        chat_sessions.pop(session_id, None)


# =========================================================
# Flask
# =========================================================

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

    except Exception as e:
        app.logger.exception("Webhook error: %s", e)
        abort(500)

    return "OK"


# =========================================================
# メッセージ受信処理
# =========================================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):

    user_message = event.message.text.strip()
    session_id = get_session_id(event)

    try:

        # 「会話をリセット」などを送ると履歴を削除
        if user_message.lower() in {
            "リセット",
            "会話をリセット",
            "履歴を削除",
            "/reset",
            "reset",
        }:
            reset_chat(session_id)

            ai_text = (
                "これまでの会話内容をリセットしました。\n"
                "新しい会話を始めましょう。"
            )

        else:
            # 同じLINEユーザーのチャットセッションを取得
            chat = get_or_create_chat(session_id)

            # 同じチャットへメッセージを追加
            response = chat.send_message(user_message)

            ai_text = response.text

            if not ai_text:
                ai_text = "申し訳ありません。回答を生成できませんでした。"

    except Exception as e:
        app.logger.exception(
            "Gemini API error. session_id=%s error=%s",
            session_id,
            e,
        )

        ai_text = "申し訳ありません。AIでエラーが発生しました。"

    try:

        with ApiClient(configuration) as api_client:

            line_bot_api = MessagingApi(api_client)

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text=ai_text)
                    ],
                )
            )

    except Exception as e:
        app.logger.exception(
            "LINE reply error. session_id=%s error=%s",
            session_id,
            e,
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port,
    )

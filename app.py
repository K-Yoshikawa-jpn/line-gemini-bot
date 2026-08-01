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

# セッション辞書の競合防止
chat_sessions_lock = threading.Lock()


# =========================================================
# 送信元・セッション管理
# =========================================================

def get_source_type(event):
    """
    LINEの送信元タイプを取得する。

    user  : 1対1トーク
    group : グループトーク
    room  : 複数人トーク
    """

    return getattr(event.source, "type", None)


def get_session_id(event):
    """
    会話履歴を保存するための識別IDを作る。

    グループでは
    「グループID + ユーザーID」
    ごとに別の会話履歴を持つ。
    """

    source = event.source

    source_type = getattr(source, "type", None)
    user_id = getattr(source, "user_id", None)
    group_id = getattr(source, "group_id", None)
    room_id = getattr(source, "room_id", None)

    if source_type == "group" and group_id:
        return f"group:{group_id}:user:{user_id or 'unknown'}"

    if source_type == "room" and room_id:
        return f"room:{room_id}:user:{user_id or 'unknown'}"

    if user_id:
        return f"user:{user_id}"

    return "unknown"


def get_or_create_chat(session_id):
    """
    ユーザーに対応したGeminiチャットを取得する。
    存在しなければ新規作成する。
    """

    with chat_sessions_lock:

        if session_id not in chat_sessions:
            chat_sessions[session_id] = client.chats.create(
                model=MODEL_NAME,
            )

        return chat_sessions[session_id]


def reset_chat(session_id):
    """
    指定ユーザーの会話履歴を削除する。
    """

    with chat_sessions_lock:
        chat_sessions.pop(session_id, None)


# =========================================================
# メンション判定
# =========================================================

def is_bot_mentioned(event):
    """
    Bot自身がメンションされているか判定する。

    1対1トークでは、メンションなしでも常にTrue。
    グループ・複数人トークでは、Botへのメンション時だけTrue。
    """

    source_type = get_source_type(event)

    # 1対1トークは常に反応
    if source_type == "user":
        return True

    # グループ・複数人トークではメンションを確認
    mention = getattr(event.message, "mention", None)

    if mention is None:
        return False

    mentionees = getattr(mention, "mentionees", None)

    if not mentionees:
        return False

    for mentionee in mentionees:

        # Python SDKでは通常 is_self
        is_self = getattr(mentionee, "is_self", False)

        # SDKのバージョン差への予備対応
        if not is_self:
            is_self = getattr(mentionee, "isSelf", False)

        if is_self:
            return True

    return False


def remove_bot_mention(event):
    """
    メッセージからBotのメンション部分を削除する。

    例：
    「@AIボット 今日の予定は？」
           ↓
    「今日の予定は？」
    """

    text = event.message.text

    mention = getattr(event.message, "mention", None)

    if mention is None:
        return text.strip()

    mentionees = getattr(mention, "mentionees", None)

    if not mentionees:
        return text.strip()

    ranges_to_remove = []

    for mentionee in mentionees:

        is_self = getattr(mentionee, "is_self", False)

        if not is_self:
            is_self = getattr(mentionee, "isSelf", False)

        if not is_self:
            continue

        index = getattr(mentionee, "index", None)
        length = getattr(mentionee, "length", None)

        if index is not None and length is not None:
            ranges_to_remove.append(
                (index, index + length)
            )

    # 後ろ側から削除すれば文字位置がずれない
    for start, end in sorted(
        ranges_to_remove,
        reverse=True,
    ):
        text = text[:start] + text[end:]

    return text.strip()


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
        app.logger.exception(
            "Webhook error: %s",
            e,
        )
        abort(500)

    return "OK"


# =========================================================
# メッセージ受信処理
# =========================================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):

    # グループ・複数人トークで
    # Botがメンションされていなければ何もしない
    if not is_bot_mentioned(event):
        app.logger.info(
            "Bot was not mentioned. Message ignored."
        )
        return

    # Botのメンション文字を削除
    user_message = remove_bot_mention(event)

    # 「@Bot」だけ送られた場合
    if not user_message:
        user_message = "何か手伝えることはありますか？"

    session_id = get_session_id(event)

    try:

        reset_commands = {
            "リセット",
            "会話をリセット",
            "履歴を削除",
            "/reset",
            "reset",
        }

        if user_message.lower() in reset_commands:

            reset_chat(session_id)

            ai_text = (
                "これまでの会話内容をリセットしました。\n"
                "新しい会話を始めましょう。"
            )

        else:

            # 同じユーザーのチャットセッションを取得
            chat = get_or_create_chat(session_id)

            # 会話を継続
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

    port = int(
        os.environ.get("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )

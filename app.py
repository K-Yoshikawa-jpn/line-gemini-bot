import os
import time
import random
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
from google.genai import errors


app = Flask(__name__)


# =========================================================
# 環境変数
# =========================================================

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]


# =========================================================
# LINE設定
# =========================================================

configuration = Configuration(
    access_token=LINE_CHANNEL_ACCESS_TOKEN
)

handler = WebhookHandler(LINE_CHANNEL_SECRET)


# =========================================================
# Gemini設定
# =========================================================

client = genai.Client(
    api_key=GEMINI_API_KEY
)

MODEL_NAME = "gemini-3.6-flash"


# =========================================================
# 会話セッション管理
# =========================================================

# LINEユーザーごとのGeminiチャットセッション
chat_sessions = {}

# ユーザーごとの送信処理を排他制御するロック
session_locks = {}

# 辞書の同時変更を防ぐロック
sessions_manager_lock = threading.Lock()


def get_source_type(event):
    """
    LINEの送信元タイプを取得する。

    user:
        1対1トーク

    group:
        グループトーク

    room:
        複数人トーク
    """

    return getattr(event.source, "type", None)


def get_session_id(event):
    """
    LINEの送信元から、会話を識別するIDを作成する。

    1対1トーク:
        user:{user_id}

    グループトーク:
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
        return (
            f"group:{group_id}:"
            f"user:{user_id or 'unknown'}"
        )

    if source_type == "room" and room_id:
        return (
            f"room:{room_id}:"
            f"user:{user_id or 'unknown'}"
        )

    if user_id:
        return f"user:{user_id}"

    return "unknown"


def get_session_lock(session_id):
    """
    ユーザーごとの排他制御用ロックを取得する。

    同じユーザーから短時間に複数メッセージが届いても、
    会話履歴が壊れないようにする。
    """

    with sessions_manager_lock:

        if session_id not in session_locks:
            session_locks[session_id] = threading.Lock()

        return session_locks[session_id]


def get_or_create_chat(session_id):
    """
    セッションIDに対応するGeminiチャットを取得する。

    存在しなければ新しいチャットを作成する。
    """

    with sessions_manager_lock:

        if session_id not in chat_sessions:
            chat_sessions[session_id] = client.chats.create(
                model=MODEL_NAME,
            )

        return chat_sessions[session_id]


def reset_chat(session_id):
    """
    指定されたユーザーの会話履歴を削除する。
    """

    with sessions_manager_lock:
        chat_sessions.pop(session_id, None)


# =========================================================
# メンション判定
# =========================================================

def is_bot_mentioned(event):
    """
    Bot自身がメンションされているか確認する。

    1対1トーク:
        メンションなしでも常に反応する。

    グループ・複数人トーク:
        Botがメンションされたときだけ反応する。
    """

    source_type = get_source_type(event)

    # 1対1トークでは常に反応
    if source_type == "user":
        return True

    mention = getattr(
        event.message,
        "mention",
        None,
    )

    if mention is None:
        return False

    mentionees = getattr(
        mention,
        "mentionees",
        None,
    )

    if not mentionees:
        return False

    for mentionee in mentionees:

        # 通常のPython SDKのプロパティ
        is_self = getattr(
            mentionee,
            "is_self",
            False,
        )

        # SDKのバージョン差への予備対応
        if not is_self:
            is_self = getattr(
                mentionee,
                "isSelf",
                False,
            )

        if is_self:
            return True

    return False


def remove_bot_mention(event):
    """
    ユーザーの文章からBot宛てメンション部分を取り除く。

    例:
        @AIボット 今日の天気は？

    Geminiへ送る文章:
        今日の天気は？
    """

    text = event.message.text

    mention = getattr(
        event.message,
        "mention",
        None,
    )

    if mention is None:
        return text.strip()

    mentionees = getattr(
        mention,
        "mentionees",
        None,
    )

    if not mentionees:
        return text.strip()

    ranges_to_remove = []

    for mentionee in mentionees:

        is_self = getattr(
            mentionee,
            "is_self",
            False,
        )

        if not is_self:
            is_self = getattr(
                mentionee,
                "isSelf",
                False,
            )

        if not is_self:
            continue

        index = getattr(
            mentionee,
            "index",
            None,
        )

        length = getattr(
            mentionee,
            "length",
            None,
        )

        if index is not None and length is not None:
            ranges_to_remove.append(
                (
                    index,
                    index + length,
                )
            )

    # 後ろから削除することで、文字位置のずれを防ぐ
    for start, end in sorted(
        ranges_to_remove,
        reverse=True,
    ):
        text = text[:start] + text[end:]

    return text.strip()


# =========================================================
# Gemini送信・自動再試行
# =========================================================

def send_message_with_retry(
    chat,
    user_message,
    session_id,
    max_attempts=3,
):
    """
    Geminiへメッセージを送信する。

    503などの一時的なサーバーエラーが発生した場合は、
    待ち時間を徐々に増やして自動再試行する。
    """

    last_error = None

    for attempt in range(1, max_attempts + 1):

        try:
            app.logger.info(
                "Sending message to Gemini. "
                "session_id=%s attempt=%s/%s",
                session_id,
                attempt,
                max_attempts,
            )

            response = chat.send_message(
                user_message
            )

            return response

        except errors.ServerError as e:
            last_error = e

            app.logger.warning(
                "Gemini server error. "
                "session_id=%s attempt=%s/%s error=%s",
                session_id,
                attempt,
                max_attempts,
                e,
            )

            # 最終回なら、そのままエラーを上位へ送る
            if attempt >= max_attempts:
                raise

            # 指数バックオフ
            # 1回目失敗後: 約1～2秒
            # 2回目失敗後: 約2～3秒
            wait_seconds = (
                2 ** (attempt - 1)
            ) + random.uniform(0, 1)

            app.logger.warning(
                "Retrying Gemini in %.1f seconds. "
                "session_id=%s",
                wait_seconds,
                session_id,
            )

            time.sleep(wait_seconds)

        except errors.ClientError:
            # 400、401、403、404、429などは、
            # 設定・モデル名・利用制限などの可能性があるため、
            # 原則として自動再試行せず上位へ送る
            raise

        except Exception:
            # 想定外のエラーも上位へ送る
            raise

    if last_error:
        raise last_error

    raise RuntimeError(
        "Geminiから応答を取得できませんでした。"
    )


# =========================================================
# LINE返信
# =========================================================

def reply_to_line(event, text):
    """
    LINEへメッセージを返信する。
    """

    # LINEのテキストメッセージ上限を考慮して切り詰める
    if len(text) > 4900:
        text = (
            text[:4900]
            + "\n\n※回答が長いため途中まで表示しています。"
        )

    with ApiClient(configuration) as api_client:

        line_bot_api = MessagingApi(api_client)

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text=text
                    )
                ],
            )
        )


# =========================================================
# Flask
# =========================================================

@app.route("/")
def home():
    return "LINE Gemini Bot is running!"


@app.route("/callback", methods=["POST"])
def callback():

    signature = request.headers.get(
        "X-Line-Signature"
    )

    if not signature:
        abort(400)

    body = request.get_data(
        as_text=True
    )

    try:
        handler.handle(
            body,
            signature,
        )

    except InvalidSignatureError:
        app.logger.warning(
            "Invalid LINE signature."
        )
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

@handler.add(
    MessageEvent,
    message=TextMessageContent,
)
def handle_message(event):

    # グループ・複数人トークで、
    # Botがメンションされていなければ何もしない
    if not is_bot_mentioned(event):

        app.logger.info(
            "Bot was not mentioned. "
            "Message ignored."
        )

        return

    # メッセージからBot宛てのメンションを取り除く
    user_message = remove_bot_mention(event)

    # 「@Bot」だけ送信された場合
    if not user_message:
        user_message = (
            "ユーザーがあなたを呼びました。"
            "自然に用件を尋ねてください。"
        )

    session_id = get_session_id(event)
    session_lock = get_session_lock(session_id)

    reset_commands = {
        "リセット",
        "会話をリセット",
        "履歴を削除",
        "/reset",
        "reset",
    }

    try:

        # 同じユーザーからの処理を1件ずつ実行する
        with session_lock:

            if user_message.lower() in reset_commands:

                reset_chat(session_id)

                ai_text = (
                    "これまでの会話内容をリセットしました。\n"
                    "新しい会話を始めましょう。"
                )

            else:

                # 同じLINEユーザーのチャットを取得
                chat = get_or_create_chat(
                    session_id
                )

                # Geminiへ送信
                # 503などの一時的なエラー時は最大3回再試行
                response = send_message_with_retry(
                    chat=chat,
                    user_message=user_message,
                    session_id=session_id,
                    max_attempts=3,
                )

                ai_text = getattr(
                    response,
                    "text",
                    None,
                )

                if not ai_text:
                    ai_text = (
                        "申し訳ありません。"
                        "回答を生成できませんでした。"
                    )

    except errors.ServerError as e:

        app.logger.exception(
            "Gemini server error after retries. "
            "session_id=%s error=%s",
            session_id,
            e,
        )

        ai_text = (
            "現在AIへのアクセスが集中しています。\n"
            "少し時間を空けて、もう一度送ってください。"
        )

    except errors.ClientError as e:

        app.logger.exception(
            "Gemini client error. "
            "session_id=%s error=%s",
            session_id,
            e,
        )

        ai_text = (
            "AIの設定または利用制限に関する"
            "エラーが発生しました。"
        )

    except Exception as e:

        app.logger.exception(
            "Gemini API error. "
            "session_id=%s error=%s",
            session_id,
            e,
        )

        ai_text = (
            "申し訳ありません。"
            "AIでエラーが発生しました。"
        )

    try:
        reply_to_line(
            event,
            ai_text,
        )

    except Exception as e:

        app.logger.exception(
            "LINE reply error. "
            "session_id=%s error=%s",
            session_id,
            e,
        )


# =========================================================
# アプリ起動
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )

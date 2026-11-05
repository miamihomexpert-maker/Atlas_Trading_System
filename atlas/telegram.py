import json
from math import isfinite
import os
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _telegram_credentials(bot_token=None, chat_id=None):
    token = bot_token or os.getenv("ATLAS_TELEGRAM_BOT_TOKEN")
    destination = chat_id or os.getenv("ATLAS_TELEGRAM_CHAT_ID")
    if not token or not destination:
        raise ValueError(
            "Set ATLAS_TELEGRAM_BOT_TOKEN and ATLAS_TELEGRAM_CHAT_ID to enable notifications."
        )
    return token, str(destination)


def send_telegram_message(message, bot_token=None, chat_id=None, timeout=10):
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Notification message cannot be empty.")
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("Notification timeout must be positive.")
    token, destination = _telegram_credentials(bot_token, chat_id)
    body = urlencode({"chat_id": destination, "text": message}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except URLError as error:
        raise RuntimeError("Telegram notification request failed.") from error
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the notification request.")
    return result["result"]


def send_trade_approval_request(
    signal,
    database,
    bot_token=None,
    chat_id=None,
    expires_minutes=15,
    timeout=10,
):
    token, destination = _telegram_credentials(bot_token, chat_id)
    approval_token = database.create_trade_approval(
        signal,
        destination,
        expires_minutes=expires_minutes,
    )
    message = (
        f"Atlas paper-trade proposal\n"
        f"{signal.get('instrument')} {signal.get('direction')}\n"
        f"Entry: {signal.get('entry_price')} | Stop: {signal.get('stop_loss')} "
        f"| Target: {signal.get('profit_target')}\n"
        f"Risk/reward: {signal.get('risk_reward_ratio')}\n"
        f"Expires in {expires_minutes} minutes.\n"
        f"Reply /approve {approval_token} or /reject {approval_token}"
    )
    try:
        send_telegram_message(
            message,
            bot_token=token,
            chat_id=destination,
            timeout=timeout,
        )
    except (RuntimeError, ValueError):
        database.cancel_trade_approval(approval_token)
        raise
    return {
        "status": "PENDING",
        "approval_token": approval_token,
        "expires_minutes": expires_minutes,
    }


def process_telegram_approval(text, chat_id, database, authorized_chat_id):
    if str(chat_id) != str(authorized_chat_id):
        return {"status": "UNAUTHORIZED"}
    parts = str(text).strip().split()
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if len(parts) != 2 or command not in {"/approve", "/reject"}:
        raise ValueError("Expected /approve TOKEN or /reject TOKEN.")
    decision = "APPROVE" if command == "/approve" else "REJECT"
    return database.process_trade_approval(parts[1], chat_id, decision)


def poll_telegram_approvals(
    database,
    bot_token=None,
    authorized_chat_id=None,
    timeout=20,
):
    """Poll Telegram once; only the configured private chat can approve/reject."""
    token, destination = _telegram_credentials(bot_token, authorized_chat_id)
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("Polling timeout must be a positive finite number.")
    offset = int(database.get_metadata("telegram_update_offset", "0"))
    query = urlencode(
        {
            "offset": offset,
            "timeout": int(timeout),
            "allowed_updates": json.dumps(["message"]),
        }
    )
    request = Request(
        f"https://api.telegram.org/bot{token}/getUpdates?{query}",
        headers={"Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout + 5) as response:
            result = json.loads(response.read().decode("utf-8"))
    except URLError as error:
        raise RuntimeError("Telegram approval poll failed.") from error
    if not result.get("ok") or not isinstance(result.get("result"), list):
        raise RuntimeError("Telegram returned an invalid approval-poll response.")

    responses = []
    next_offset = offset
    for update in result["result"]:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(next_offset, update_id + 1)
        message = update.get("message", {})
        chat = message.get("chat", {})
        text = message.get("text")
        if (
            chat.get("type") == "private"
            and str(chat.get("id")) == destination
            and isinstance(text, str)
            and text.strip().split()
            and text.strip().split()[0].lower().split("@", 1)[0]
            in {"/approve", "/reject"}
        ):
            responses.append(
                process_telegram_approval(
                    text,
                    chat.get("id"),
                    database,
                    destination,
                )
            )
    if next_offset != offset:
        database.set_metadata("telegram_update_offset", next_offset)
    return responses


def get_telegram_status():
    if os.getenv("ATLAS_TELEGRAM_BOT_TOKEN") and os.getenv("ATLAS_TELEGRAM_CHAT_ID"):
        return "TELEGRAM CREDENTIALS CONFIGURED; SEND AND APPROVAL POLLING ARE EXPLICIT"
    return "TELEGRAM NOT CONFIGURED"

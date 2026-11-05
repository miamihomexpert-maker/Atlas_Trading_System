from io import BytesIO
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import pytest

from atlas.broker import PaperBroker
from atlas.database import AtlasDatabase
from atlas.execution import execute_paper_signal
from atlas.telegram import poll_telegram_approvals, send_trade_approval_request


def test_telegram_notification_and_remote_approval_flow(monkeypatch):
    database = AtlasDatabase(":memory:")
    signal = {
        "instrument": "XYZ",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "profit_target": 120.0,
        "risk_reward_ratio": 2.0,
    }
    sent_message = {}

    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/sendMessage"):
            sent_message.update(
                parse_qs(request.data.decode("utf-8"))
            )
            response = {"ok": True, "result": {"message_id": 1}}
        else:
            text = sent_message["text"][0]
            approval_token = text.split("/approve ", 1)[1].split()[0]
            response = {
                "ok": True,
                "result": [
                    {
                        "update_id": 4,
                        "message": {
                            "chat": {"id": 123, "type": "private"},
                            "text": f"/approve {approval_token}",
                        },
                    }
                ],
            }
        return BytesIO(json.dumps(response).encode("utf-8"))

    monkeypatch.setattr("atlas.telegram.urlopen", fake_urlopen)
    request = send_trade_approval_request(
        signal,
        database,
        bot_token="test-token",
        chat_id="123",
    )
    assert request["status"] == "PENDING"
    assert "Atlas paper-trade proposal" in sent_message["text"][0]

    responses = poll_telegram_approvals(
        database,
        bot_token="test-token",
        authorized_chat_id="123",
        timeout=1,
    )

    assert responses == [{"status": "APPROVED", "signal": signal}]
    assert database.get_metadata("telegram_update_offset") == "5"
    database.close()


def test_approval_is_restricted_to_authorized_chat_and_single_use():
    database = AtlasDatabase(":memory:")
    signal = {"instrument": "XYZ", "direction": "BUY"}
    token = database.create_trade_approval(signal, "123")

    unauthorized = database.process_trade_approval(token, "999", "APPROVE")
    approved = database.process_trade_approval(token, "123", "APPROVE")
    repeated = database.process_trade_approval(token, "123", "REJECT")

    assert unauthorized["status"] == "UNAUTHORIZED"
    assert approved["status"] == "APPROVED"
    assert repeated["status"] == "APPROVED"
    database.close()


def test_remote_approval_only_executes_the_exact_approved_paper_signal():
    database = AtlasDatabase(":memory:")
    signal = {
        "instrument": "XYZ",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "profit_target": 120.0,
        "risk_reward_ratio": 2.0,
        "explanation": "Approved test proposal",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    token = database.create_trade_approval(signal, "123")
    broker = PaperBroker(initial_cash=10_000)

    with pytest.raises(PermissionError, match="PENDING"):
        execute_paper_signal(
            signal,
            broker,
            "remote-order",
            approval_token=token,
            approval_chat_id="123",
            approval_database=database,
        )

    database.process_trade_approval(token, "123", "APPROVE")
    changed_signal = dict(signal, entry_price=101.0)
    with pytest.raises(PermissionError, match="SIGNAL_MISMATCH"):
        execute_paper_signal(
            changed_signal,
            broker,
            "remote-order",
            approval_token=token,
            approval_chat_id="123",
            approval_database=database,
        )

    order = execute_paper_signal(
        signal,
        broker,
        "remote-order",
        approval_token=token,
        approval_chat_id="123",
        approval_database=database,
    )
    assert order["status"] == "FILLED"
    assert database.get_trade_approval(token, "123")["status"] == "EXECUTED"
    database.close()

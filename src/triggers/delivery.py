"""Send only to a trigger's authenticated chat, with durable revocation checks.

This adapter owns no receiver, WebSocket, retry loop or account state. A report
worker can reuse the regular card builders without broadcasting a personal
trigger to the globally configured email/webhook destinations.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import requests

from ..core.report_execution import runtime_path
from ..interactive.security import BotAccess
from ..notification.feishu_notifier import FeishuNotifier, _build_interactive_card
from ..notification.receipts import legacy_tuple
from ..notification.settings import env_flag

FEISHU_API = "https://open.feishu.cn/open-apis"


def binding_status(config: dict, *, run_id: str | None = None) -> str:
    """Return allowed/revoked/invalid from the shared committed action and run."""
    binding = config.get("_trigger_context")
    if not isinstance(binding, dict):
        return "invalid"
    try:
        path = runtime_path(config).resolve()
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        ) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT a.*,d.state AS trigger_state,d.revision AS current_revision,"
                "r.job_id,r.status AS run_status,r.payload AS run_payload "
                "FROM trigger_actions a JOIN trigger_definitions d USING(owner,number) "
                "LEFT JOIN execution_runs r ON r.id=a.run_id WHERE a.id=?",
                (binding.get("action_id"),),
            ).fetchone()
            ids = binding.get("action_ids", [binding.get("action_id")])
            if (
                not isinstance(ids, list)
                or not 1 <= len(ids) <= 100
                or any(not isinstance(item, str) for item in ids)
                or binding.get("action_id") not in ids
                or len(set(ids)) != len(ids)
            ):
                return "invalid"
            members = db.execute(
                "SELECT * FROM trigger_actions WHERE id IN ("
                + ",".join("?" for _ in ids)
                + ")",
                ids,
            ).fetchall()
        if row is None:
            return "invalid"
        if len(members) != len(ids) or any(
            member["owner"] != row["owner"]
            or member["number"] != row["number"]
            or member["revision"] != row["revision"]
            or member["kind"] != row["kind"]
            or member["action_index"] != row["action_index"]
            or member["payload"] != row["payload"]
            or member["run_id"] != row["run_id"]
            or json.loads(member["payload"]).get("chat_id") != binding.get("chat_id")
            for member in members
        ):
            return "invalid"
        payload = json.loads(row["payload"])
        queued = json.loads(row["run_payload"] or "{}")
        expected = (
            "triggers.reminder"
            if row["kind"] == "reminder"
            else "reports." + row["kind"]
        )
        stored_binding = (
            queued.get("binding")
            if row["kind"] == "reminder"
            else queued.get("config_snapshot", {}).get("_trigger_context")
        )
        if (
            binding != stored_binding
            or binding.get("owner") != row["owner"]
            or binding.get("number") != row["number"]
            or binding.get("revision") != row["revision"]
            or binding.get("kind") != row["kind"]
            or binding.get("chat_id") != payload.get("chat_id")
            or row["job_id"] != expected
            or (run_id is not None and row["run_id"] != run_id)
        ):
            return "invalid"
        if (
            row["trigger_state"] != "active"
            or row["current_revision"] != row["revision"]
            or row["cancel_requested"]
            or row["state"] != "submitted"
            or row["run_status"] != "running"
            or any(
                member["state"] != "submitted" or member["cancel_requested"]
                for member in members
            )
        ):
            return "revoked"
        return "allowed"
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        return "invalid"


class TriggerChatNotifier(FeishuNotifier):
    """Reuse full report formatting, replacing only its transport destination."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.access = BotAccess(config, "feishu", allow_wildcard=True)
        settings = (config.get("interactive") or {}).get("feishu") or {}
        self.app_id = settings.get("app_id") or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = settings.get("app_secret") or os.getenv(
            "FEISHU_APP_SECRET", ""
        )
        self.binding = config.get("_trigger_context") or {}
        self._token = ""

    def send_reminder(self, text: str) -> dict:
        return self._post_card(
            {
                "msg_type": "interactive",
                "card": _build_interactive_card(
                    f"触发器 #{self.binding.get('number', '?')}", text
                ),
            }
        )

    def _send_card(self, payload: dict, *, classified: bool = False):
        result = self._post_card(payload)
        return result if classified else legacy_tuple(result)

    def _guard(self):
        state = binding_status(self.config)
        if state != "allowed":
            return {
                "status": "skipped" if state == "revoked" else "failed",
                "reason": "trigger_revoked"
                if state == "revoked"
                else "trigger_binding_invalid",
            }
        if env_flag("SKIP_NOTIFICATIONS") or env_flag("SKIP_FEISHU"):
            return {"status": "skipped", "reason": "channel_disabled"}
        try:
            from ..core.config_store import ConfigStore

            current = ConfigStore(Path(self.binding["config_path"])).load_runtime()
            if runtime_path(current).resolve() != runtime_path(self.config).resolve():
                return {"status": "failed", "reason": "trigger_profile_changed"}
            access = BotAccess(current, "feishu", allow_wildcard=True)
            settings = (current.get("interactive") or {}).get("feishu") or {}
            app_id = settings.get("app_id") or os.getenv("FEISHU_APP_ID", "")
            secret = settings.get("app_secret") or os.getenv("FEISHU_APP_SECRET", "")
            if app_id != self.app_id or secret != self.app_secret:
                return {"status": "failed", "reason": "trigger_credentials_changed"}
        except (OSError, ValueError, TypeError, KeyError):
            return {"status": "failed", "reason": "trigger_configuration_unavailable"}
        if not access.enabled or not access.gate.is_allowed(
            self.binding.get("chat_id")
        ):
            return {"status": "failed", "reason": "trigger_chat_unauthorized"}
        if not self.app_id or not self.app_secret:
            return {"status": "failed", "reason": "configuration_missing"}
        return None

    def _post_card(self, payload: dict) -> dict:
        guard = self._guard()
        if guard:
            return guard
        try:
            card = deepcopy(payload["card"])
            # Feishu Card 2.0 components belong to body.elements. A second
            # top-level elements array makes otherwise valid reports invalid.
            if (
                not isinstance(card, dict)
                or card.get("schema") != "2.0"
                or "elements" in card
                or not isinstance(card.get("body"), dict)
                or not isinstance(card["body"].get("elements"), list)
            ):
                raise ValueError("invalid Feishu Card 2.0 envelope")
            # Every piece carries its reason; no extra untracked notification.
            reason = self.binding.get("reason", "")
            if reason:
                card["body"]["elements"] = [
                    {"tag": "markdown", "content": reason},
                    *card["body"]["elements"],
                ]
            content = json.dumps(card, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, KeyError):
            return {"status": "failed", "reason": "payload_invalid"}
        if not self._token:
            try:
                response = requests.post(
                    FEISHU_API + "/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    timeout=(5, 5),
                    allow_redirects=False,
                )
                data = response.json() if response.status_code == 200 else {}
                token = (
                    data.get("tenant_access_token") if data.get("code") == 0 else None
                )
                if not isinstance(token, str) or not token:
                    return {
                        "status": "failed",
                        "reason": "feishu_authentication_failed",
                    }
                self._token = token
            except (requests.RequestException, ValueError, TypeError, AttributeError):
                # This endpoint cannot have delivered the user message.
                return {
                    "status": "failed",
                    "reason": "feishu_authentication_unavailable",
                }
        guard = self._guard()  # Deletion during token acquisition must win.
        if guard:
            return guard
        try:
            response = requests.post(
                FEISHU_API + "/im/v1/messages?receive_id_type=chat_id",
                json={
                    "receive_id": self.binding["chat_id"],
                    "msg_type": "interactive",
                    "content": content,
                },
                headers={"Authorization": "Bearer " + self._token},
                timeout=(5, 10),
                allow_redirects=False,
            )
        except (
            requests.ConnectTimeout,
            requests.exceptions.InvalidURL,
            requests.exceptions.InvalidHeader,
        ):
            return {"status": "failed", "reason": "http_pre_submit_failed"}
        except Exception as exc:  # noqa: BLE001 - never resend an ambiguous submission
            return {
                "status": "unknown",
                "reason": "http_submission_unknown",
                "error": type(exc).__name__,
            }
        if response.status_code != 200:
            rejected = response.status_code in {
                400,
                401,
                403,
                404,
                405,
                413,
                415,
                422,
                429,
            }
            return {
                "status": "failed" if rejected else "unknown",
                "reason": "http_rejected" if rejected else "http_response_uncertain",
                "http_status": response.status_code,
                **_platform_error_metadata(response),
            }
        try:
            result = response.json()
            code = result.get("code")
            if type(code) is not int:
                raise ValueError("acknowledgement missing")
        except (ValueError, TypeError, AttributeError):
            return {"status": "unknown", "reason": "http_acknowledgement_missing"}
        if code != 0:
            return {
                "status": "failed",
                "reason": "feishu_rejected",
                "code": code,
                **_platform_error_metadata(response),
            }
        message = result.get("data") or {}
        return {
            "status": "sent",
            "reason": "delivery_acknowledged",
            "platform_message_id": message.get("message_id")
            if isinstance(message, dict)
            else None,
        }


def _platform_error_metadata(response) -> dict:
    """Keep support identifiers, never API bodies or credential-bearing text."""
    metadata = {}
    try:
        data = response.json()
    except (ValueError, TypeError, AttributeError):
        data = None
    if isinstance(data, dict):
        if type(data.get("code")) is int:
            metadata["code"] = data["code"]
        error = data.get("error")
        if isinstance(error, dict):
            log_id = error.get("log_id")
            if isinstance(log_id, str) and re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", log_id
            ):
                metadata["platform_log_id"] = log_id
    return metadata

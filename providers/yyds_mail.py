from __future__ import annotations

import abc
import html
import re
import time
from typing import Any, Callable, Optional

import requests


class ProviderError(Exception):
    """Raised when the mail provider cannot complete a step."""


class EmailProvider(abc.ABC):
    @abc.abstractmethod
    def create_inbox(self) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def get_address(self) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def list_messages(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    def read_message(self, message_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def wait_for_code(
        self,
        timeout: int = 180,
        interval: int = 5,
        progress: Optional[Callable[[int, int, int], None]] = None,
    ) -> str:
        raise NotImplementedError


def _strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def _first_value(payload: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, "", [], {}):
                return value
        for nested_key in ("data", "result", "account", "inbox", "message", "item"):
            nested = payload.get(nested_key)
            value = _first_value(nested, keys)
            if value not in (None, "", [], {}):
                return value
    return None


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "items", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        nested = _extract_items(value)
        if nested:
            return nested
    return []


def _collect_text_blobs(payload: Any) -> list[str]:
    blobs: list[str] = []
    if isinstance(payload, str):
        text = _strip_html(payload) if "<" in payload else payload.strip()
        if text:
            blobs.append(text)
        return blobs
    if isinstance(payload, list):
        for item in payload:
            blobs.extend(_collect_text_blobs(item))
        return blobs
    if isinstance(payload, dict):
        interesting = (
            "subject",
            "snippet",
            "preview",
            "body",
            "text",
            "textBody",
            "plain",
            "plainText",
            "html",
            "htmlBody",
            "content",
            "from",
            "sender",
        )
        for key in interesting:
            if key in payload:
                blobs.extend(_collect_text_blobs(payload[key]))
        for value in payload.values():
            if isinstance(value, (dict, list)):
                blobs.extend(_collect_text_blobs(value))
        return blobs
    return blobs


def _message_sort_key(message: dict[str, Any]) -> tuple[str, str]:
    created = (
        message.get("createdAt")
        or message.get("created_at")
        or message.get("receivedAt")
        or message.get("received_at")
        or message.get("date")
        or ""
    )
    return str(created), str(message.get("id", ""))


def _extract_code(text: str) -> Optional[str]:
    matches = list(re.finditer(r"\b(\d{6})\b", text))
    if not matches:
        return None
    lowered = text.lower()
    keywords = ("windsurf", "verification", "verify", "code", "login", "验证码")

    def score(match: re.Match[str]) -> tuple[int, int]:
        start = max(0, match.start() - 120)
        end = min(len(lowered), match.end() + 120)
        window = lowered[start:end]
        return (sum(1 for keyword in keywords if keyword in window), -match.start())

    best = max(matches, key=score)
    return best.group(1)


class YYDSMailProvider(EmailProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        session: Optional[requests.Session] = None,
        request_timeout: int = 20,
        domain: Optional[str] = None,
        subdomain: Optional[str] = None,
        local_part: Optional[str] = None,
    ) -> None:
        if not api_key:
            raise ProviderError("缺少 YYDS Mail API key")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.request_timeout = request_timeout
        self.domain = domain
        self.subdomain = subdomain
        self.local_part = local_part
        self.inbox_id = ""
        self.address = ""
        self.temp_token = ""

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-API-Key": self.api_key,
        }
        if self.temp_token:
            headers["Authorization"] = f"Bearer {self.temp_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        response = self.session.request(
            method=method,
            url=f"{self.base_url}{path}",
            headers=self._headers(),
            params=params,
            json=json_body,
            timeout=self.request_timeout,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"YYDS Mail 返回了非 JSON 数据: HTTP {response.status_code}"
            ) from exc
        if not response.ok:
            message = payload.get("error") if isinstance(payload, dict) else payload
            code = payload.get("errorCode") if isinstance(payload, dict) else None
            detail = f"{message} ({code})" if code else str(message)
            raise ProviderError(detail)
        if isinstance(payload, dict) and payload.get("success") is False:
            message = payload.get("error") or "未知错误"
            code = payload.get("errorCode")
            detail = f"{message} ({code})" if code else str(message)
            raise ProviderError(detail)
        return payload

    def create_inbox(self) -> str:
        payload: dict[str, Any] = {}
        if self.local_part:
            payload["localPart"] = self.local_part
        if self.domain:
            payload["domain"] = self.domain
        if self.subdomain:
            payload["subdomain"] = self.subdomain

        response = self._request("POST", "/accounts", json_body=payload or {})
        self.inbox_id = str(_first_value(response, ("id", "accountId", "mailboxId")) or "")
        self.address = str(
            _first_value(response, ("address", "email", "mailbox", "account"))
            or ""
        )
        self.temp_token = str(
            _first_value(response, ("tempToken", "token", "accessToken")) or ""
        )
        if not self.address:
            raise ProviderError("创建邮箱成功但响应里没有 address")
        return self.address

    def get_address(self) -> str:
        if not self.address:
            raise ProviderError("邮箱尚未创建")
        return self.address

    def list_messages(self) -> list[dict[str, Any]]:
        address = self.get_address()
        response = self._request("GET", "/messages", params={"address": address})
        return _extract_items(response)

    def read_message(self, message_id: str) -> dict[str, Any]:
        address = self.get_address()
        response = self._request(
            "GET",
            f"/messages/{message_id}",
            params={"address": address},
        )
        if isinstance(response, dict):
            return response
        raise ProviderError("邮件详情响应格式不正确")

    def wait_for_code(
        self,
        timeout: int = 180,
        interval: int = 5,
        progress: Optional[Callable[[int, int, int], None]] = None,
    ) -> str:
        started_at = time.time()
        deadline = time.time() + timeout
        seen_ids: set[str] = set()
        while time.time() < deadline:
            messages = sorted(self.list_messages(), key=_message_sort_key, reverse=True)
            if progress:
                progress(int(time.time() - started_at), timeout, len(messages))
            for message in messages:
                message_id = str(
                    message.get("id")
                    or message.get("_id")
                    or message.get("messageId")
                    or ""
                )
                if not message_id or message_id in seen_ids:
                    continue
                seen_ids.add(message_id)

                try:
                    detail = self.read_message(message_id)
                except ProviderError:
                    detail = message
                combined = "\n".join(_collect_text_blobs(detail) + _collect_text_blobs(message))
                code = _extract_code(combined)
                if code:
                    return code
            time.sleep(interval)
        raise ProviderError(f"{timeout} 秒内没有收到可用验证码")

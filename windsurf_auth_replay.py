#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Windsurf automation CLI.

Capabilities:
1. Create a YYDS Mail inbox
2. Register a new Windsurf account
3. Auto-read the verification code
4. Exchange auth1 -> devin-session-token -> ott
5. Upload ott to a remote WindsurfPoolAPI instance

Default pool upload mode uses:
  POST /auth/login

Optional dashboard mode uses:
  POST /dashboard/api/accounts
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import random
import re
import string
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import requests

from providers.yyds_mail import ProviderError, YYDSMailProvider


def load_dotenv(path: str = ".env") -> None:
    """Load simple KEY=VALUE pairs without adding a python-dotenv dependency."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class WorkflowError(Exception):
    """Raised when a workflow step fails with a user-facing message."""


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else default


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def env_optional_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def first_nonempty(*values: Any) -> Optional[Any]:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def mask_secret(value: str, head: int = 8, tail: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= head + tail + 3:
        return value
    return f"{value[:head]}...{value[-tail:]}"


def encode_varint(value: int) -> bytes:
    chunks = []
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            chunks.append(byte | 0x80)
        else:
            chunks.append(byte)
            break
    return bytes(chunks)


def encode_proto_string(field_number: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    tag = (field_number << 3) | 2
    return encode_varint(tag) + encode_varint(len(raw)) + raw


def encode_proto_varint_field(field_number: int, value: int) -> bytes:
    tag = field_number << 3
    return encode_varint(tag) + encode_varint(value)


def decode_varint(data: bytes, start: int) -> tuple[int, int]:
    value = 0
    shift = 0
    pos = start
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, pos
        shift += 7
    raise WorkflowError("解析 protobuf varint 失败: 数据提前结束")


def skip_proto_value(data: bytes, pos: int, wire_type: int) -> int:
    if wire_type == 0:
        _, pos = decode_varint(data, pos)
        return pos
    if wire_type == 1:
        pos += 8
    elif wire_type == 2:
        length, pos = decode_varint(data, pos)
        pos += length
    elif wire_type == 5:
        pos += 4
    else:
        raise WorkflowError(f"暂不支持的 protobuf wire type: {wire_type}")
    if pos > len(data):
        raise WorkflowError("解析 protobuf 失败: 字段长度越界")
    return pos


def decode_proto_bool_field(data: bytes, field_number: int) -> Optional[bool]:
    pos = 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        current_field = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:
            value, pos = decode_varint(data, pos)
            if current_field == field_number:
                return bool(value)
            continue
        pos = skip_proto_value(data, pos, wire_type)
    return None


def build_proto_message(*parts: tuple[int, str]) -> bytes:
    body = b""
    for field_number, value in parts:
        if value:
            body += encode_proto_string(field_number, value)
    return body


def detect_trial_plan_candidates(config: "AppConfig") -> list[str]:
    candidates: list[str] = []
    if config.trial_plan_id:
        candidates.append(config.trial_plan_id.strip())
    parsed = urlparse(config.turnstile_site_url)
    query_plan = parse_qs(parsed.query).get("plan", [])
    for value in query_plan:
        normalized = value.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    if "9" not in candidates:
        candidates.append("9")
    return candidates


def extract_checkout_url(content: bytes | str) -> str:
    text = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else content
    match = re.search(r"https://checkout\.stripe\.com/[^\s\x00\"']+", text)
    if not match:
        raise WorkflowError("生成 Stripe Checkout 链接失败: 响应里没有找到 checkout URL")
    return match.group(0)


def extract_ascii_token(content: bytes, pattern: bytes, context: str) -> str:
    match = re.search(pattern, content)
    if not match:
        preview = content[:200].decode("utf-8", errors="replace")
        raise WorkflowError(f"{context}失败: 没有从响应中提取到目标 token，响应预览: {preview!r}")
    return match.group(1).decode("utf-8", errors="strict")


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(length))


def generate_name() -> str:
    adjectives = ("Swift", "Bright", "Quiet", "Lucky", "Clever", "Fresh", "Solid", "Rapid")
    nouns = ("River", "Falcon", "Panda", "Maple", "Orbit", "Pixel", "Stone", "Cloud")
    suffix = "".join(random.SystemRandom().choice(string.digits) for _ in range(4))
    return f"{random.choice(adjectives)} {random.choice(nouns)} {suffix}"


def prompt_value(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def prompt_password() -> str:
    password = getpass.getpass("请输入注册密码（直接回车则自动生成）: ").strip()
    if password:
        return password
    password = generate_password()
    print(f"[*] 已自动生成密码: {password}")
    return password


def prompt_login_password() -> str:
    password = getpass.getpass("请输入已有账号的登录密码: ").strip()
    if not password:
        raise WorkflowError("trial 模式缺少登录密码，或请直接提供 session token")
    return password


def prompt_account_count(value: Optional[int]) -> int:
    if value is not None:
        if value < 1:
            raise WorkflowError("账号数量必须大于等于 1")
        return value
    if not sys.stdin.isatty():
        return 1
    raw = prompt_value("请输入需要注册的账号数量", default="1")
    try:
        count = int(raw)
    except ValueError as exc:
        raise WorkflowError("账号数量必须是数字") from exc
    if count < 1:
        raise WorkflowError("账号数量必须大于等于 1")
    return count


def normalize_windsurf_base_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    parsed = urlparse(base_url)
    if parsed.hostname == "www.windsurf.com":
        scheme = parsed.scheme or "https"
        return f"{scheme}://windsurf.com"
    return base_url


def maybe_json(response: requests.Response) -> Optional[Any]:
    try:
        return response.json()
    except ValueError:
        return None


def extract_error_message(response: requests.Response) -> str:
    payload = maybe_json(response)
    if isinstance(payload, dict):
        detail = first_nonempty(
            payload.get("error"),
            payload.get("message"),
            payload.get("detail"),
        )
        if isinstance(detail, dict):
            detail = first_nonempty(detail.get("message"), json.dumps(detail, ensure_ascii=False))
        if detail:
            code = payload.get("errorCode")
            return f"{detail} ({code})" if code else str(detail)
    text = response.text.strip()
    return text or f"HTTP {response.status_code}"


def raise_for_http(response: requests.Response, context: str) -> None:
    if response.ok:
        return
    raise WorkflowError(f"{context}失败: {extract_error_message(response)}")


def print_step(message: str) -> None:
    print(f"[*] {message}")


def print_success(message: str) -> None:
    print(f"[+] {message}")


def print_warn(message: str) -> None:
    print(f"[!] {message}")


@dataclass
class AppConfig:
    base_url: str
    pool_base_url: str
    pool_upload_mode: str
    pool_dashboard_password: str
    pool_ssh_key_path: str
    pool_ssh_user: str
    yyds_base_url: str
    yyds_api_key: str
    yyds_domain: str
    yyds_subdomain: str
    yyds_local_part: str
    request_timeout: int
    poll_timeout: int
    poll_interval: int
    max_attempts: int
    verify_ssl: bool
    debug: bool
    generate_trial_link: bool
    turnstile_solver_url: str
    turnstile_token: str
    turnstile_site_url: str
    turnstile_sitekey: str
    turnstile_browser_path: str
    turnstile_timeout: int
    turnstile_headless: bool
    trial_success_url: str
    trial_cancel_url: str
    trial_plan_id: str
    trial_check_session_field: int
    trial_eligible_field: int
    trial_sub_session_field: int
    trial_sub_success_field: int
    trial_sub_cancel_field: int
    trial_sub_turnstile_field: int
    trial_sub_plan_id_field: int


class WindsurfClient:
    def __init__(
        self,
        base_url: str,
        session: Optional[requests.Session] = None,
        request_timeout: int = 20,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.request_timeout = request_timeout
        self.verify_ssl = verify_ssl

    def _json_headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/account/register",
            "Content-Type": "application/json",
        }

    def _proto_headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/account/register",
            "Content-Type": "application/proto",
        }

    def _seat_service_url(self, method: str) -> str:
        return (
            f"{self.base_url}"
            f"/_backend/exa.seat_management_pb.SeatManagementService/{method}"
        )

    def check_email_status(self, email: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/_devin-auth/connections",
            headers=self._json_headers(),
            json={"product": "windsurf", "email": email},
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "检查邮箱状态")
        payload = maybe_json(response)
        if not isinstance(payload, dict):
            raise WorkflowError("检查邮箱状态失败: 返回了非 JSON 数据")
        return payload

    def password_login_supported(self, email: str) -> bool:
        payload = self.check_email_status(email)
        auth_method = payload.get("auth_method", {}) if isinstance(payload, dict) else {}
        has_password = None
        if isinstance(auth_method, dict):
            has_password = auth_method.get("has_password")
        if has_password is None and isinstance(payload, dict):
            has_password = payload.get("has_password")
        return bool(has_password)

    def login_with_password(self, email: str, password: str) -> str:
        response = self.session.post(
            f"{self.base_url}/_devin-auth/password/login",
            headers=self._json_headers(),
            json={"email": email, "password": password},
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "密码登录")
        payload = maybe_json(response)
        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise WorkflowError("密码登录失败: 响应里没有 auth1 token")
        return token

    def request_verification_code(self, email: str) -> str:
        response = self.session.post(
            f"{self.base_url}/_devin-auth/email/start",
            headers=self._json_headers(),
            json={"email": email, "mode": "signup", "product": "Windsurf"},
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "发送验证码")
        payload = maybe_json(response)
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise WorkflowError(f"发送验证码失败: {payload}")
        token = payload.get("email_verification_token")
        if not token:
            raise WorkflowError("发送验证码失败: 没有拿到 email_verification_token")
        return token

    def complete_registration(
        self,
        email_verification_token: str,
        code: str,
        password: str,
        name: str,
    ) -> str:
        response = self.session.post(
            f"{self.base_url}/_devin-auth/email/complete",
            headers=self._json_headers(),
            json={
                "email_verification_token": email_verification_token,
                "code": code,
                "mode": "signup",
                "password": password,
                "name": name,
            },
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "完成注册")
        payload = maybe_json(response)
        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise WorkflowError("完成注册失败: 响应里没有 auth1 token")
        return token

    def exchange_for_session(self, auth1_token: str) -> str:
        response = self.session.post(
            self._seat_service_url("WindsurfPostAuth"),
            headers=self._proto_headers(),
            data=encode_proto_string(1, auth1_token),
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "兑换 session")
        return extract_ascii_token(
            response.content,
            rb"(devin-session-token\$[A-Za-z0-9._~+/=-]+)",
            "兑换 session",
        )

    def get_one_time_token(self, session_token: str) -> str:
        response = self.session.post(
            self._seat_service_url("GetOneTimeAuthToken"),
            headers=self._proto_headers(),
            data=encode_proto_string(1, session_token),
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "获取 OTT")
        return extract_ascii_token(
            response.content,
            rb"(ott\$[A-Za-z0-9._-]+)",
            "获取 OTT",
        )

    def check_trial_eligibility(self, session_token: str, config: AppConfig) -> bool:
        url = self._seat_service_url("CheckProTrialEligibility")
        candidates = [
            build_proto_message((config.trial_check_session_field, session_token)),
            session_token.encode("utf-8"),
            b"",
        ]
        last_error = ""
        for payload in candidates:
            response = self.session.post(
                url,
                headers=self._proto_headers(),
                data=payload,
                timeout=self.request_timeout,
                verify=self.verify_ssl,
            )
            if not response.ok:
                last_error = extract_error_message(response)
                continue
            eligible = decode_proto_bool_field(response.content, config.trial_eligible_field)
            if eligible is not None:
                return eligible
            text = response.text.strip().lower()
            if text in {"true", "false"}:
                return text == "true"
            last_error = (
                "返回成功但无法解析资格响应，预览: "
                f"{response.content[:120].decode('utf-8', errors='replace')!r}"
            )
        raise WorkflowError(f"检查 Trial 资格失败: {last_error or '无可用响应'}")

    def create_trial_checkout_url(
        self,
        session_token: str,
        turnstile_token: str,
        config: AppConfig,
    ) -> str:
        base_payload = build_proto_message(
            (config.trial_sub_session_field, session_token),
            (config.trial_sub_success_field, config.trial_success_url),
            (config.trial_sub_cancel_field, config.trial_cancel_url),
            (config.trial_sub_turnstile_field, turnstile_token),
        )
        plan_candidates = detect_trial_plan_candidates(config)
        payload_variants: list[tuple[str, bytes]] = [("no_plan_id", base_payload)]
        for plan_id in plan_candidates:
            payload_variants.append(
                (
                    f"plan_id_string:{plan_id}",
                    base_payload + encode_proto_string(config.trial_sub_plan_id_field, plan_id),
                )
            )
            if plan_id.isdigit():
                payload_variants.append(
                    (
                        f"plan_id_varint:{plan_id}",
                        base_payload
                        + encode_proto_varint_field(config.trial_sub_plan_id_field, int(plan_id)),
                    )
                )

        tried_variants: set[tuple[str, bytes]] = set()
        errors: list[str] = []
        url = self._seat_service_url("SubscribeToPlan")
        for variant_name, payload in payload_variants:
            dedupe_key = (variant_name, payload)
            if dedupe_key in tried_variants:
                continue
            tried_variants.add(dedupe_key)
            response = self.session.post(
                url,
                headers=self._proto_headers(),
                data=payload,
                timeout=self.request_timeout,
                verify=self.verify_ssl,
            )
            if response.ok:
                return extract_checkout_url(response.content)
            errors.append(f"{variant_name} -> {extract_error_message(response)}")

        joined = " | ".join(errors)
        raise WorkflowError(f"生成 Stripe Checkout 链接失败: {joined}")


class WindsurfPoolClient:
    def __init__(
        self,
        base_url: str,
        session: Optional[requests.Session] = None,
        request_timeout: int = 20,
        verify_ssl: bool = True,
        upload_mode: str = "auth",
        dashboard_password: str = "",
        ssh_key_path: str = "~/.ssh/id_ed25519",
        ssh_user: str = "root",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.request_timeout = request_timeout
        self.verify_ssl = verify_ssl
        self.upload_mode = upload_mode
        self.dashboard_password = dashboard_password
        self.ssh_key_path = os.path.expanduser(ssh_key_path)
        self.ssh_user = ssh_user

    def upload_token(self, token: str, label: str = "") -> dict[str, Any]:
        if self.upload_mode == "dashboard":
            return self.upload_via_dashboard(token, label=label)
        return self.upload_via_auth_login(token, label=label)

    def upload_via_auth_login(self, token: str, label: str = "") -> dict[str, Any]:
        payload = {"token": token}
        if label:
            payload["label"] = label
        response = self.session.post(
            f"{self.base_url}/auth/login",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "上传 token 到 WindsurfPoolAPI")
        payload = maybe_json(response)
        if not isinstance(payload, dict):
            raise WorkflowError("上传 token 到 WindsurfPoolAPI 失败: 返回了非 JSON 数据")
        return payload

    def upload_via_dashboard(self, token: str, label: str = "") -> dict[str, Any]:
        payload = {"token": token, "label": label}
        response = self.session.post(
            f"{self.base_url}/dashboard/api/accounts",
            headers={
                "Content-Type": "application/json",
                "X-Dashboard-Password": self.resolve_dashboard_password(),
            },
            json=payload,
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "通过 dashboard 上传 token")
        payload = maybe_json(response)
        if not isinstance(payload, dict):
            raise WorkflowError("通过 dashboard 上传 token 失败: 返回了非 JSON 数据")
        return payload

    def list_accounts(self) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/auth/accounts",
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "读取 WindsurfPoolAPI 账户列表")
        payload = maybe_json(response)
        if not isinstance(payload, dict):
            return []
        accounts = payload.get("accounts")
        return accounts if isinstance(accounts, list) else []

    def health(self) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/health",
            timeout=self.request_timeout,
            verify=self.verify_ssl,
        )
        raise_for_http(response, "读取 WindsurfPoolAPI 健康状态")
        payload = maybe_json(response)
        return payload if isinstance(payload, dict) else {}

    def resolve_dashboard_password(self) -> str:
        if self.dashboard_password:
            return self.dashboard_password
        raise WorkflowError(
            "缺少 dashboard 密码，请通过 --pool-dashboard-password "
            "或 WINDSURF_POOL_DASHBOARD_PASSWORD 手动提供"
        )


def create_provider(config: AppConfig, session: requests.Session) -> YYDSMailProvider:
    if not config.yyds_api_key:
        raise WorkflowError(
            "没有配置 YYDS mail API key。请设置环境变量 YYDS_MAIL_API_KEY "
            "或通过命令行参数传入。"
        )
    return YYDSMailProvider(
        base_url=config.yyds_base_url,
        api_key=config.yyds_api_key,
        session=session,
        request_timeout=config.request_timeout,
        domain=config.yyds_domain or None,
        subdomain=config.yyds_subdomain or None,
        local_part=config.yyds_local_part or None,
    )


def resolve_browser_executable_path(explicit_path: str = "") -> str:
    candidates = []
    if explicit_path:
        candidates.append(os.path.expanduser(explicit_path))
    candidates.extend(
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    )
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


async def async_solve_turnstile_token(
    site_url: str,
    sitekey: str = "",
    browser_path: str = "",
    timeout: int = 90,
    headless: bool = True,
) -> str:
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:
        raise WorkflowError(
            "缺少 patchright 依赖，无法在本地浏览器里解 Turnstile。"
            "请先执行 pip install patchright，或改用 TURNSTILE_SOLVER_URL / WINDSURF_TURNSTILE_TOKEN。"
        ) from exc

    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    resolved_browser = resolve_browser_executable_path(browser_path)
    if resolved_browser:
        launch_kwargs["executable_path"] = resolved_browser

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_kwargs)
        page = await browser.new_page()
        try:
            await page.goto(site_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            await page.wait_for_timeout(1500)

            for frame in page.frames:
                if "challenges.cloudflare.com" not in frame.url:
                    continue
                for selector in ("#checkbox", "input[type=checkbox]", "label.ctp-checkbox-label"):
                    locator = frame.locator(selector)
                    try:
                        if await locator.count():
                            await locator.first.click(timeout=5000)
                            break
                    except Exception:
                        continue

            token_script = """
            () => {
              const hidden = document.querySelector('[name="cf-turnstile-response"]');
              if (hidden && hidden.value) return hidden.value;
              try {
                if (window.turnstile && typeof window.turnstile.getResponse === "function") {
                  const value = window.turnstile.getResponse();
                  if (value) return value;
                }
              } catch (err) {}
              return "";
            }
            """
            for _ in range(max(1, timeout)):
                token = await page.evaluate(token_script)
                if token:
                    return token
                await page.wait_for_timeout(1000)
        except Exception as exc:
            try:
                await page.screenshot(path="turnstile_error.png")
            except Exception:
                pass
            detail = f"{exc}. 如需排查可查看 turnstile_error.png"
            if sitekey:
                detail += f"，当前 sitekey={sitekey}"
            raise WorkflowError(f"本地 Turnstile 求解失败: {detail}") from exc
        finally:
            await browser.close()
    raise WorkflowError("本地 Turnstile 求解失败: 在超时时间内没有拿到 token")


def solve_turnstile_token_with_options(
    site_url: str,
    sitekey: str = "",
    browser_path: str = "",
    timeout: int = 90,
    headless: bool = True,
) -> str:
    return asyncio.run(
        async_solve_turnstile_token(
            site_url=site_url,
            sitekey=sitekey,
            browser_path=browser_path,
            timeout=timeout,
            headless=headless,
        )
    )


def resolve_turnstile_token(config: AppConfig) -> tuple[str, str]:
    if config.turnstile_token:
        return config.turnstile_token, "env"
    if config.turnstile_solver_url:
        response = requests.post(
            config.turnstile_solver_url,
            json={
                "site_url": config.turnstile_site_url,
                "sitekey": config.turnstile_sitekey,
                "browser_path": config.turnstile_browser_path,
                "timeout": config.turnstile_timeout,
                "headless": config.turnstile_headless,
            },
            timeout=config.request_timeout,
            verify=config.verify_ssl,
        )
        raise_for_http(response, "请求外部 Turnstile solver")
        payload = maybe_json(response)
        if not isinstance(payload, dict) or not payload.get("token"):
            raise WorkflowError("请求外部 Turnstile solver 失败: 响应里没有 token")
        return str(payload["token"]), "solver_url"
    token = solve_turnstile_token_with_options(
        site_url=config.turnstile_site_url,
        sitekey=config.turnstile_sitekey,
        browser_path=config.turnstile_browser_path,
        timeout=config.turnstile_timeout,
        headless=config.turnstile_headless,
    )
    return token, "browser"


def _browser_trial_fallback(
    config: AppConfig,
    email: str,
    password: str,
    login_url: str = "",
    billing_url: str = "",
    headless: bool = False,
) -> dict[str, Any]:
    base_url = config.base_url
    login_url = login_url or f"{base_url}/account/login"
    billing_url = billing_url or config.turnstile_site_url
    timeout = max(10, config.turnstile_timeout)

    checkout_url = asyncio.run(
        _async_run_browser_trial(
            email=email,
            password=password,
            login_url=login_url,
            billing_url=billing_url,
            browser_path=config.turnstile_browser_path,
            timeout=timeout,
            headless=headless,
        )
    )
    return {
        "trial_eligible": True,
        "trial_checkout_url": checkout_url,
        "turnstile_token_source": "browser-fallback",
    }


def generate_trial_checkout(
    windsurf: WindsurfClient,
    config: AppConfig,
    session_token: str,
    email: str = "",
    password: str = "",
    args: Optional[argparse.Namespace] = None,
) -> dict[str, Any]:
    if email and password:
        print_step("已跳过 Trial API 方式，直接使用浏览器自动化生成链接")
        login_url = getattr(args, "login_url", "") if args else ""
        billing_url = getattr(args, "billing_url", "") if args else ""
        headless = getattr(args, "headless_browser", False) if args else False
        return _browser_trial_fallback(
            config,
            email=email,
            password=password,
            login_url=login_url,
            billing_url=billing_url,
            headless=headless,
        )

    try:
        print_step("正在检查 Pro Trial 资格")
        eligible = windsurf.check_trial_eligibility(session_token, config)
        print_success(f"Trial 资格检查完成: {'eligible' if eligible else 'ineligible'}")
        if not eligible:
            raise WorkflowError("账号不符合 Trial 资格")

        print_step("正在获取 Turnstile token")
        turnstile_token, token_source = resolve_turnstile_token(config)
        print_success(f"Turnstile token 已获取 (source={token_source})")

        print_step("正在生成 Stripe Checkout 链接")
        checkout_url = windsurf.create_trial_checkout_url(session_token, turnstile_token, config)
        print_success("Stripe Checkout 链接已生成")
        return {
            "trial_eligible": eligible,
            "trial_checkout_url": checkout_url,
            "turnstile_token_source": token_source,
        }
    except WorkflowError as api_exc:
        if not email or not password:
            raise
        print_warn(f"API 方式生成 Trial 链接失败: {api_exc}")
        print_step("正在降级到浏览器自动化方式")
        login_url = getattr(args, "login_url", "") if args else ""
        billing_url = getattr(args, "billing_url", "") if args else ""
        headless = getattr(args, "headless_browser", False) if args else False
        return _browser_trial_fallback(
            config, email, password,
            login_url=login_url,
            billing_url=billing_url,
            headless=headless,
        )


EMAIL_INPUT_SELECTORS = (
    "input[type='email']",
    "input[name='email']",
    "input[autocomplete='email']",
)

PASSWORD_INPUT_SELECTORS = (
    "input[type='password']",
    "input[name='password']",
    "input[autocomplete='current-password']",
)

NEXT_BUTTON_SELECTORS = (
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Sign in')",
    "button:has-text('Log in')",
    "button[type='submit']",
)

PASSWORD_SUBMIT_SELECTORS = (
    "button:has-text('Continue')",
    "button:has-text('Sign in')",
    "button:has-text('Log in')",
    "button:has-text('Continue with Email')",
    "button[type='submit']",
)

TRIAL_BUTTON_SELECTORS = (
    "button:has-text('Start Free Trial')",
    "button:has-text('Start trial')",
    "button:has-text('Try Pro')",
    "button:has-text('Upgrade')",
    "button:has-text('Continue')",
    "button:has-text('Subscribe')",
    "button:has-text('Checkout')",
    "button:has-text('Select plan')",
    "a:has-text('Start trial')",
    "a:has-text('Try Pro')",
    "a:has-text('Upgrade')",
)


@dataclass
class CheckoutCapture:
    checkout_url: str = ""
    subscribe_errors: list[str] = field(default_factory=list)
    turnstile_token: str = ""


async def _browser_maybe_accept_cookies(page) -> bool:
    for selector in (
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Allow all')",
    ):
        locator = page.locator(selector)
        try:
            if await locator.count():
                await locator.first.click(timeout=1000)
                return True
        except Exception:
            continue
    return False


async def _browser_fill_first(page, selectors: tuple[str, ...], value: str, timeout_s: int = 15) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if await locator.count():
                    await locator.first.fill(value, timeout=1500)
                    return selector
            except Exception:
                continue
        await page.wait_for_timeout(300)
    raise WorkflowError(f"没有找到可填写的输入框: {selectors}")


async def _browser_click_first(page, selectors: tuple[str, ...], timeout_s: int = 20) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if await locator.count():
                    await locator.first.click(timeout=1500)
                    return selector
            except Exception:
                continue
        await page.wait_for_timeout(300)
    raise WorkflowError(f"没有找到可点击的元素: {selectors}")


async def _browser_get_local_storage(page, key: str) -> str:
    value = await page.evaluate(
        """(storageKey) => {
        try {
          return window.localStorage.getItem(storageKey) || "";
        } catch (err) {
          return "";
        }
      }""",
        key,
    )
    return str(value or "")


async def _browser_login(page, email: str, password: str, timeout_s: int) -> None:
    await _browser_maybe_accept_cookies(page)
    email_selector = await _browser_fill_first(page, EMAIL_INPUT_SELECTORS, email, timeout_s=timeout_s)
    print_success(f"邮箱已填入 ({email_selector})")

    try:
        password_selector = await _browser_fill_first(page, PASSWORD_INPUT_SELECTORS, password, timeout_s=3)
        print_success(f"密码已填入 ({password_selector})")
    except WorkflowError:
        next_selector = await _browser_click_first(page, NEXT_BUTTON_SELECTORS, timeout_s=8)
        print_success(f"已点击下一步 ({next_selector})")
        password_selector = await _browser_fill_first(page, PASSWORD_INPUT_SELECTORS, password, timeout_s=timeout_s)
        print_success(f"密码已填入 ({password_selector})")

    password_input = page.locator(PASSWORD_INPUT_SELECTORS[0])
    if not await password_input.count():
        for selector in PASSWORD_INPUT_SELECTORS[1:]:
            password_input = page.locator(selector)
            if await password_input.count():
                break
    submitted = False
    try:
        if await password_input.count():
            await password_input.first.press("Enter", timeout=1500)
            submitted = True
            print_success("已通过密码框回车提交登录")
    except Exception:
        submitted = False

    if not submitted:
        submit_selector = await _browser_click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout_s=timeout_s)
        print_success(f"已点击登录按钮 ({submit_selector})")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        session_token = await _browser_get_local_storage(page, "devin_session_token")
        auth1_token = await _browser_get_local_storage(page, "devin_auth1_token")
        account_id = await _browser_get_local_storage(page, "devin_account_id")
        org_id = await _browser_get_local_storage(page, "devin_primary_org_id")
        if session_token and auth1_token and (account_id or org_id or "account/login" not in page.url):
            print_success("浏览器登录完成，登录态已写入 localStorage")
            return
        if "checkout.stripe.com" in page.url:
            return
        await page.wait_for_timeout(500)
    raise WorkflowError("浏览器登录后没有检测到完整登录态")


async def _browser_click_turnstile_checkbox(page) -> bool:
    clicked = False
    for frame in page.frames:
        if "challenges.cloudflare.com" not in frame.url:
            continue
        for selector in ("#checkbox", "input[type='checkbox']", "label.ctp-checkbox-label"):
            locator = frame.locator(selector)
            try:
                if await locator.count():
                    await locator.first.click(timeout=1500)
                    clicked = True
                    break
            except Exception:
                continue
    return clicked


async def _browser_wait_for_turnstile_token(page, timeout_s: int, capture: CheckoutCapture) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await _browser_maybe_accept_cookies(page)
        parsed = urlparse(page.url)
        query_token = parse_qs(parsed.query).get("turnstile_token", [])
        if query_token and query_token[0]:
            capture.turnstile_token = query_token[0]
            return capture.turnstile_token

        token = await page.evaluate(
            """() => {
            const hidden = document.querySelector('[name="cf-turnstile-response"]');
            if (hidden && hidden.value) return hidden.value;
            try {
              if (window.turnstile && typeof window.turnstile.getResponse === "function") {
                return window.turnstile.getResponse() || "";
              }
            } catch (err) {}
            return "";
          }"""
        )
        token = str(token or "")
        if token:
            capture.turnstile_token = token
            return token

        await _browser_click_turnstile_checkbox(page)
        await page.wait_for_timeout(1000)

    try:
        await page.screenshot(path="trial_browser_turnstile_timeout.png")
    except Exception:
        pass
    raise WorkflowError(
        "等待 Turnstile token 超时。已保存截图到 trial_browser_turnstile_timeout.png"
    )


def _browser_install_response_capture(page, capture: CheckoutCapture) -> None:
    async def handle_response(response) -> None:
        try:
            if "SubscribeToPlan" in response.url:
                text = await response.text()
                checkout_url = extract_checkout_url_text(text)
                if checkout_url:
                    capture.checkout_url = checkout_url
                elif text.strip():
                    capture.subscribe_errors.append(text.strip()[:500])
        except Exception as exc:
            capture.subscribe_errors.append(f"读取 SubscribeToPlan 响应失败: {exc}")

    def on_response(response) -> None:
        asyncio.create_task(handle_response(response))

    page.on("response", on_response)


def extract_checkout_url_text(text: str) -> str:
    match = re.search(r"https://checkout\.stripe\.com/[^\s\x00\"']+", text)
    return match.group(0) if match else ""


async def _browser_wait_for_checkout(page, capture: CheckoutCapture, timeout_s: int) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if capture.checkout_url:
            return capture.checkout_url
        if page.url.startswith("https://checkout.stripe.com/"):
            return page.url
        for opened_page in page.context.pages:
            if opened_page.url.startswith("https://checkout.stripe.com/"):
                return opened_page.url
        await page.wait_for_timeout(500)
    detail = " | ".join(capture.subscribe_errors[-3:]) if capture.subscribe_errors else "未捕获到 Stripe URL"
    raise WorkflowError(f"等待 Stripe Checkout URL 超时: {detail}")


async def _async_run_browser_trial(
    email: str,
    password: str,
    login_url: str,
    billing_url: str,
    browser_path: str,
    timeout: int,
    headless: bool,
) -> str:
    try:
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError
        from patchright.async_api import async_playwright
    except ImportError as exc:
        raise WorkflowError(
            "缺少 patchright 依赖，无法使用 trial-browser 模式。"
            "请先执行 pip install patchright。"
        ) from exc

    resolved_browser = resolve_browser_executable_path(browser_path)
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if resolved_browser:
        launch_kwargs["executable_path"] = resolved_browser

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        page = await context.new_page()
        capture = CheckoutCapture()
        _browser_install_response_capture(page, capture)

        try:
            print_step(f"正在打开登录页: {login_url}")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            await _browser_login(page, email, password, timeout_s=timeout)
            await page.wait_for_timeout(2000)

            print_step(f"正在进入 Trial 页面: {billing_url}")
            await page.goto(billing_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            await _browser_maybe_accept_cookies(page)
            await page.wait_for_timeout(1500)
            if "account/login" in page.url:
                raise WorkflowError("进入 Trial 页面后被重定向回登录页，说明浏览器登录态还没建立完整")

            try:
                initial_click = await _browser_click_first(page, TRIAL_BUTTON_SELECTORS, timeout_s=5)
                print_success(f"已预先点击 Trial 按钮 ({initial_click})")
            except WorkflowError:
                pass

            print_step("正在等待并点击 Turnstile")
            turnstile_token = await _browser_wait_for_turnstile_token(page, timeout, capture)
            print_success(f"Turnstile token 已获取: {turnstile_token[:24]}...")

            if capture.checkout_url:
                return capture.checkout_url

            print_step("正在点击 Trial / Subscribe 按钮")
            clicked_selector = await _browser_click_first(page, TRIAL_BUTTON_SELECTORS, timeout_s=timeout)
            print_success(f"已点击订阅按钮 ({clicked_selector})")

            checkout_url = await _browser_wait_for_checkout(page, capture, timeout_s=timeout)
            print_success("Stripe Checkout URL 已捕获")
            return checkout_url
        except PlaywrightTimeoutError as exc:
            try:
                await page.screenshot(path="trial_browser_error.png")
            except Exception:
                pass
            raise WorkflowError(
                f"浏览器流程超时: {exc}. 已保存截图到 trial_browser_error.png"
            ) from exc
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def trial_browser_workflow(config: AppConfig, args: argparse.Namespace) -> dict[str, Any]:
    email = args.email
    password = args.password

    if not email:
        raise WorkflowError("trial-browser 模式需要 --email")
    if not password:
        if sys.stdin.isatty():
            password = prompt_login_password()
        else:
            raise WorkflowError("trial-browser 模式需要 --password")

    base_url = config.base_url
    login_url = args.login_url or f"{base_url}/account/login"
    billing_url = args.billing_url or config.turnstile_site_url
    browser_path = config.turnstile_browser_path
    timeout = max(10, config.turnstile_timeout)
    headless = getattr(args, "headless_browser", False)

    checkout_url = asyncio.run(
        _async_run_browser_trial(
            email=email,
            password=password,
            login_url=login_url,
            billing_url=billing_url,
            browser_path=browser_path,
            timeout=timeout,
            headless=headless,
        )
    )
    return {
        "mode": "trial-browser",
        "email": email,
        "trial_checkout_url": checkout_url,
    }


def is_retryable_registration_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "no eligible organizations found" in message
        or "没有收到可用验证码" in message
        or "收信超时" in message
    )


def print_code_wait_progress(elapsed: int, timeout: int, message_count: int) -> None:
    remaining = max(0, timeout - elapsed)
    print_step(
        "等待验证码中 "
        f"({elapsed}/{timeout}s, 剩余 {remaining}s, 邮件数 {message_count})"
    )


def resolve_account_name(args: argparse.Namespace, index: int, total: int) -> str:
    if args.name:
        return args.name if total == 1 else f"{args.name} {index}"
    name = generate_name()
    print_step(f"第 {index}/{total} 个账号未填写昵称，已自动生成: {name}")
    return name


def run_registration_attempt(
    config: AppConfig,
    args: argparse.Namespace,
    session: requests.Session,
    windsurf: WindsurfClient,
    pool: WindsurfPoolClient,
    name: str,
    password: str,
    attempt: int,
) -> dict[str, Any]:
    if config.max_attempts > 1:
        print_step(f"开始第 {attempt}/{config.max_attempts} 次注册尝试")

    provider = create_provider(config, session)
    try:
        print_step("正在申请 YYDS mail 邮箱")
        email = provider.create_inbox()
        print_success(f"邮箱已创建: {email}")
    except ProviderError as exc:
        raise WorkflowError(f"邮箱创建失败: {exc}") from exc

    print_step("正在检查邮箱状态")
    status = windsurf.check_email_status(email)
    auth_method = (
        status.get("auth_method", {}).get("method")
        if isinstance(status, dict)
        else None
    )
    if auth_method and auth_method != "not_found":
        raise WorkflowError(f"目标邮箱似乎已经被注册，auth_method={auth_method}")
    print_success("邮箱可用于新注册")

    print_step("正在发送验证码")
    email_verification_token = windsurf.request_verification_code(email)
    print_success("验证码已发送")

    print_step("正在等待验证码邮件")
    try:
        code = provider.wait_for_code(
            timeout=config.poll_timeout,
            interval=config.poll_interval,
            progress=print_code_wait_progress,
        )
    except ProviderError as exc:
        raise WorkflowError(f"收信超时或解析失败: {exc}") from exc
    print_success(f"已收到验证码: {code}")

    print_step("正在完成注册")
    auth1_token = windsurf.complete_registration(
        email_verification_token=email_verification_token,
        code=code,
        password=password,
        name=name,
    )
    print_success(f"auth1 token 已获取: {mask_secret(auth1_token)}")

    print_step("正在兑换业务 session")
    session_token = windsurf.exchange_for_session(auth1_token)
    print_success(f"session 已获取: {mask_secret(session_token)}")

    print_step("正在生成 OTT")
    ott = windsurf.get_one_time_token(session_token)
    print_success(f"OTT 已获取: {mask_secret(ott)}")

    label = args.label or name or email
    print_step(
        "正在上传 OTT 到 WindsurfPoolAPI "
        f"({config.pool_base_url}, mode={config.pool_upload_mode})"
    )
    upload_result = pool.upload_token(ott, label=label)
    print_success("OTT 上传完成")

    accounts = []
    try:
        accounts = pool.list_accounts()
    except WorkflowError as exc:
        print_warn(str(exc))

    result = {
        "mode": "full",
        "name": name,
        "password": password,
        "email": email,
        "auth1_token": auth1_token,
        "session_token": session_token,
        "ott": ott,
        "pool_result": upload_result,
        "pool_accounts_total": len(accounts),
    }
    if config.generate_trial_link:
        result.update(generate_trial_checkout(
            windsurf, config, session_token,
            email=email, password=password, args=args,
        ))
    return result


def full_workflow(config: AppConfig, args: argparse.Namespace) -> dict[str, Any]:
    if not config.pool_base_url:
        raise WorkflowError("缺少 WindsurfPoolAPI 地址，请通过 --pool-base-url 或 WINDSURF_POOL_URL 手动提供")
    account_count = prompt_account_count(args.account_count)
    print_step(f"本轮计划注册 {account_count} 个账号")
    password = args.password or prompt_password()

    session = requests.Session()
    windsurf = WindsurfClient(
        base_url=config.base_url,
        session=session,
        request_timeout=config.request_timeout,
        verify_ssl=config.verify_ssl,
    )
    pool = WindsurfPoolClient(
        base_url=config.pool_base_url,
        session=session,
        request_timeout=config.request_timeout,
        verify_ssl=config.verify_ssl,
        upload_mode=config.pool_upload_mode,
        dashboard_password=config.pool_dashboard_password,
        ssh_key_path=config.pool_ssh_key_path,
        ssh_user=config.pool_ssh_user,
    )

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for account_index in range(1, account_count + 1):
        print_step(f"========== 正在注册第 {account_index}/{account_count} 个账号 ==========")
        name = resolve_account_name(args, account_index, account_count)
        last_error: Optional[WorkflowError] = None
        account_success = False
        for attempt in range(1, config.max_attempts + 1):
            try:
                result = run_registration_attempt(
                    config=config,
                    args=args,
                    session=session,
                    windsurf=windsurf,
                    pool=pool,
                    name=name,
                    password=password,
                    attempt=attempt,
                )
                result["batch_index"] = account_index
                results.append(result)
                account_success = True
                break
            except WorkflowError as exc:
                last_error = exc
                if attempt < config.max_attempts and is_retryable_registration_error(exc):
                    print_warn(
                        "当前邮箱注册后没有可用组织，自动换一个邮箱重试。"
                        f"原始错误: {exc}"
                    )
                    continue
                break

        if not account_success:
            reason = str(last_error or "未知错误")
            print_warn(f"第 {account_index}/{account_count} 个账号注册失败，已跳过。原因: {reason}")
            failures.append(
                {
                    "batch_index": account_index,
                    "name": name,
                    "error": reason,
                }
            )

    if account_count == 1 and results and not failures:
        return results[0]
    return {
        "mode": "batch",
        "requested_count": account_count,
        "success_count": len(results),
        "failure_count": len(failures),
        "accounts": results,
        "failures": failures,
        "pool_accounts_total": results[-1].get("pool_accounts_total") if results else 0,
    }


def upload_only_workflow(config: AppConfig, args: argparse.Namespace) -> dict[str, Any]:
    if not config.pool_base_url:
        raise WorkflowError("缺少 WindsurfPoolAPI 地址，请通过 --pool-base-url 或 WINDSURF_POOL_URL 手动提供")
    ott = args.ott or prompt_value("请输入要上传的 OTT")
    if not ott:
        raise WorkflowError("没有提供 OTT")

    session = requests.Session()
    pool = WindsurfPoolClient(
        base_url=config.pool_base_url,
        session=session,
        request_timeout=config.request_timeout,
        verify_ssl=config.verify_ssl,
        upload_mode=config.pool_upload_mode,
        dashboard_password=config.pool_dashboard_password,
        ssh_key_path=config.pool_ssh_key_path,
        ssh_user=config.pool_ssh_user,
    )

    print_step(
        "正在上传 OTT 到 WindsurfPoolAPI "
        f"({config.pool_base_url}, mode={config.pool_upload_mode})"
    )
    upload_result = pool.upload_token(ott, label=args.label or "")
    print_success("OTT 上传完成")

    accounts = []
    health = {}
    try:
        accounts = pool.list_accounts()
    except WorkflowError as exc:
        print_warn(str(exc))
    try:
        health = pool.health()
    except WorkflowError as exc:
        print_warn(str(exc))

    return {
        "mode": "upload",
        "ott": ott,
        "pool_result": upload_result,
        "pool_accounts_total": len(accounts),
        "pool_health": health,
    }


def trial_workflow(config: AppConfig, args: argparse.Namespace) -> dict[str, Any]:
    session = requests.Session()
    windsurf = WindsurfClient(
        base_url=config.base_url,
        session=session,
        request_timeout=config.request_timeout,
        verify_ssl=config.verify_ssl,
    )

    auth1_token = ""
    session_token = args.session_token
    email = args.email
    password = args.password

    if not session_token:
        if not email:
            raise WorkflowError("trial 模式需要 --email，或者直接提供 --session-token")
        if not password:
            if sys.stdin.isatty():
                password = prompt_login_password()
            else:
                raise WorkflowError("trial 模式缺少登录密码，或请直接提供 --session-token")
        print_step("正在检查账号是否支持密码登录")
        if not windsurf.password_login_supported(email):
            raise WorkflowError("该邮箱当前不支持密码登录")
        print_step("正在执行密码登录")
        auth1_token = windsurf.login_with_password(email, password)
        print_success(f"auth1 token 已获取: {mask_secret(auth1_token)}")
        print_step("正在兑换业务 session")
        session_token = windsurf.exchange_for_session(auth1_token)
        print_success(f"session 已获取: {mask_secret(session_token)}")

    result = {
        "mode": "trial",
        "email": email,
        "auth1_token": auth1_token,
        "session_token": session_token,
    }
    result.update(generate_trial_checkout(
        windsurf, config, session_token,
        email=email or "", password=password or "", args=args,
    ))
    return result


def summarize_result(result: dict[str, Any], include_secrets: bool = False) -> dict[str, Any]:
    summary = dict(result)
    if include_secrets:
        return summary
    if isinstance(summary.get("accounts"), list):
        summary["accounts"] = [
            summarize_result(item, include_secrets=False) if isinstance(item, dict) else item
            for item in summary["accounts"]
        ]
    if isinstance(summary.get("failures"), list):
        summary["failures"] = [
            summarize_result(item, include_secrets=False) if isinstance(item, dict) else item
            for item in summary["failures"]
        ]
    if "password" in summary and isinstance(summary["password"], str):
        summary["password"] = mask_secret(summary["password"], 3, 2)
    for field in ("auth1_token", "session_token", "ott"):
        if field in summary and isinstance(summary[field], str):
            summary[field] = mask_secret(summary[field])
    return summary


def print_final_summary(result: dict[str, Any], show_secrets: bool = False) -> None:
    if isinstance(result.get("accounts"), list):
        print("\n=== 批量完成 ===")
        print(f"计划数量: {result.get('requested_count', len(result['accounts']))}")
        print(f"成功数量: {result.get('success_count', len(result['accounts']))}")
        print(f"失败数量: {result.get('failure_count', len(result.get('failures', [])))}")
        for account in result["accounts"]:
            if not isinstance(account, dict):
                continue
            pool_result = account.get("pool_result") or {}
            pool_account = pool_result.get("account") if isinstance(pool_result, dict) else {}
            print(
                "账号 "
                f"{account.get('batch_index', '')}: "
                f"{account.get('email', '')} / "
                f"{pool_account.get('id', '')} / "
                f"{pool_account.get('status', '')}"
            )
        for failure in result.get("failures", []):
            if not isinstance(failure, dict):
                continue
            print(
                "失败 "
                f"{failure.get('batch_index', '')}: "
                f"{failure.get('name', '')} / "
                f"{failure.get('error', '')}"
            )
        if result.get("pool_accounts_total") is not None:
            print(f"Pool 当前账户数: {result['pool_accounts_total']}")
        return

    pool_result = result.get("pool_result") or {}
    account = pool_result.get("account") if isinstance(pool_result, dict) else None
    print("\n=== 完成 ===")
    if result.get("email"):
        print(f"邮箱: {result['email']}")
    if result.get("password"):
        print(
            "注册密码: "
            f"{result['password'] if show_secrets else mask_secret(result['password'], 3, 2)}"
        )
    if result.get("ott"):
        print(f"OTT: {result['ott'] if show_secrets else mask_secret(result['ott'])}")
    if result.get("trial_eligible") is not None:
        print(f"Trial 资格: {'eligible' if result['trial_eligible'] else 'ineligible'}")
    if result.get("trial_checkout_url"):
        print(f"Stripe Checkout URL: {result['trial_checkout_url']}")
    if isinstance(account, dict):
        print(f"Pool 账户 ID: {account.get('id', '')}")
        print(f"Pool 账户状态: {account.get('status', '')}")
        if account.get("email"):
            print(f"Pool 显示名: {account.get('email')}")
    if result.get("pool_accounts_total") is not None:
        print(f"Pool 当前账户数: {result['pool_accounts_total']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="自动注册 Windsurf、上传 OTT，并可一体化生成 Pro Trial Stripe 链接。",
    )
    parser.add_argument(
        "--mode",
        choices=("full", "upload", "trial", "trial-browser"),
        default=env_str("WINDSURF_WORKFLOW_MODE", "full"),
        help="full=完整注册流程，upload=只上传现成 OTT，trial=API 生成 Pro Trial 链接，trial-browser=浏览器自动化 Trial 流程。",
    )
    parser.add_argument("--email", default=env_str("WINDSURF_EMAIL"))
    parser.add_argument("--name", default=env_str("WINDSURF_NAME"))
    parser.add_argument("--password", default=env_str("WINDSURF_PASSWORD"))
    parser.add_argument("--label", default=env_str("WINDSURF_POOL_LABEL"))
    parser.add_argument("--ott", default=env_str("WINDSURF_OTT"))
    parser.add_argument("--session-token", default=env_str("WINDSURF_SESSION_TOKEN"))
    parser.add_argument(
        "--account-count",
        type=int,
        default=env_optional_int("WINDSURF_ACCOUNT_COUNT"),
        help="完整注册模式下本轮要注册的账号数量。未填写时启动后询问。",
    )
    parser.add_argument(
        "--base-url",
        default=env_str("WINDSURF_BASE_URL", "https://windsurf.com"),
        help="Windsurf 站点基地址。",
    )
    parser.add_argument(
        "--pool-base-url",
        default=env_str("WINDSURF_POOL_URL"),
        help="WindsurfPoolAPI 地址。无默认值，需手动提供。",
    )
    parser.add_argument(
        "--pool-upload-mode",
        choices=("auth", "dashboard"),
        default=env_str("WINDSURF_POOL_UPLOAD_MODE", "auth"),
        help="auth 走 /auth/login，dashboard 走 /dashboard/api/accounts。",
    )
    parser.add_argument(
        "--pool-dashboard-password",
        default=env_str("WINDSURF_POOL_DASHBOARD_PASSWORD"),
        help="dashboard 模式下的 X-Dashboard-Password。",
    )
    parser.add_argument(
        "--pool-ssh-key-path",
        default=env_str("WINDSURF_POOL_SSH_KEY_PATH", "~/.ssh/id_ed25519"),
        help="dashboard 模式缺少密码时，用于 SSH 读取远端密码的私钥路径。",
    )
    parser.add_argument(
        "--pool-ssh-user",
        default=env_str("WINDSURF_POOL_SSH_USER", "root"),
        help="dashboard 模式下通过 SSH 读取远端密码时使用的用户名。",
    )
    parser.add_argument(
        "--yyds-base-url",
        default=env_str("YYDS_MAIL_BASE_URL", "https://maliapi.215.im/v1"),
        help="YYDS Mail API 基地址。",
    )
    parser.add_argument("--yyds-api-key", default=env_str("YYDS_MAIL_API_KEY"))
    parser.add_argument("--yyds-domain", default=env_str("YYDS_MAIL_DOMAIN"))
    parser.add_argument("--yyds-subdomain", default=env_str("YYDS_MAIL_SUBDOMAIN"))
    parser.add_argument("--yyds-local-part", default=env_str("YYDS_MAIL_LOCAL_PART"))
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=env_int("REQUEST_TIMEOUT", 20),
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=env_int("POLL_TIMEOUT", 60),
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=env_int("POLL_INTERVAL", 5),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=env_int("MAX_ATTEMPTS", 5),
        help="完整注册模式下的最大自动重试次数。",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=not env_bool("VERIFY_SSL", True),
        help="禁用 SSL 证书校验，适合 CTF 或自签环境。",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=env_bool("DEBUG", False),
        help="调试模式，输出更多信息。",
    )
    parser.add_argument(
        "--generate-trial-link",
        action="store_true",
        default=env_bool("WINDSURF_GENERATE_TRIAL_LINK", False),
        help="完整注册模式下，注册并上传 OTT 后继续生成 Pro Trial Stripe 链接。",
    )
    parser.add_argument(
        "--turnstile-token",
        default=env_str("WINDSURF_TURNSTILE_TOKEN"),
        help="已知的 Turnstile token。提供后会跳过浏览器求解。",
    )
    parser.add_argument(
        "--turnstile-site-url",
        default=env_str("WINDSURF_TURNSTILE_SITE_URL"),
        help="Turnstile 页面地址。默认自动拼接为 /billing/individual?plan=9。",
    )
    parser.add_argument(
        "--turnstile-sitekey",
        default=env_str("WINDSURF_TURNSTILE_SITEKEY"),
        help="可选的 Turnstile sitekey，仅用于调试输出。",
    )
    parser.add_argument(
        "--turnstile-solver-url",
        default=env_str("TURNSTILE_SOLVER_URL"),
        help="可选的外部 solver 地址，例如 http://127.0.0.1:3000/solve。",
    )
    parser.add_argument(
        "--turnstile-browser-path",
        default=env_str("TURNSTILE_BROWSER_PATH"),
        help="本地浏览器可执行文件路径。为空时自动尝试常见 Chrome/Chromium 路径。",
    )
    parser.add_argument(
        "--turnstile-timeout",
        type=int,
        default=env_int("TURNSTILE_TIMEOUT", 90),
        help="等待 Turnstile token 的超时时间（秒）。",
    )
    parser.add_argument(
        "--headed-turnstile",
        action="store_true",
        default=not env_bool("TURNSTILE_HEADLESS", True),
        help="用有界面浏览器运行 Turnstile 求解。",
    )
    parser.add_argument(
        "--login-url",
        default="",
        help="trial-browser 模式的登录页地址。默认 {base_url}/account/login。",
    )
    parser.add_argument(
        "--billing-url",
        default="",
        help="trial-browser 模式的 Trial 页地址。默认使用 turnstile-site-url。",
    )
    parser.add_argument(
        "--headless-browser",
        action="store_true",
        default=False,
        help="trial-browser 模式使用无头浏览器。默认有界面，便于观察。",
    )
    parser.add_argument(
        "--trial-success-url",
        default=env_str("WINDSURF_TRIAL_SUCCESS_URL"),
        help="SubscribeToPlan 的 success_url。默认自动拼接。",
    )
    parser.add_argument(
        "--trial-cancel-url",
        default=env_str("WINDSURF_TRIAL_CANCEL_URL"),
        help="SubscribeToPlan 的 cancel_url。默认自动拼接。",
    )
    parser.add_argument(
        "--trial-plan-id",
        default=env_str("WINDSURF_TRIAL_PLAN_ID"),
        help="可选的 trial plan_id。目标要求时再填写。",
    )
    parser.add_argument(
        "--output-json",
        default=env_str("OUTPUT_JSON"),
        help="把运行结果写到 JSON 文件。",
    )
    parser.add_argument(
        "--include-secrets-in-output",
        action="store_true",
        default=False,
        help="配合 --output-json 使用，把完整密码和 token 一起写入结果文件。",
    )
    parser.add_argument(
        "--show-secrets",
        action="store_true",
        default=env_bool("SHOW_SECRETS", False),
        help="在终端摘要中显示完整密码和 token。默认只显示脱敏值。",
    )
    return parser


def build_config(args: argparse.Namespace) -> AppConfig:
    base_url = normalize_windsurf_base_url(args.base_url)
    return AppConfig(
        base_url=base_url,
        pool_base_url=args.pool_base_url,
        pool_upload_mode=args.pool_upload_mode,
        pool_dashboard_password=args.pool_dashboard_password,
        pool_ssh_key_path=args.pool_ssh_key_path,
        pool_ssh_user=args.pool_ssh_user,
        yyds_base_url=args.yyds_base_url,
        yyds_api_key=args.yyds_api_key,
        yyds_domain=args.yyds_domain,
        yyds_subdomain=args.yyds_subdomain,
        yyds_local_part=args.yyds_local_part,
        request_timeout=args.request_timeout,
        poll_timeout=args.poll_timeout,
        poll_interval=args.poll_interval,
        max_attempts=max(1, args.max_attempts),
        verify_ssl=not args.insecure,
        debug=args.debug,
        generate_trial_link=args.generate_trial_link,
        turnstile_solver_url=args.turnstile_solver_url,
        turnstile_token=args.turnstile_token,
        turnstile_site_url=args.turnstile_site_url or f"{base_url}/billing/individual?plan=9",
        turnstile_sitekey=args.turnstile_sitekey,
        turnstile_browser_path=args.turnstile_browser_path,
        turnstile_timeout=max(5, args.turnstile_timeout),
        turnstile_headless=not args.headed_turnstile,
        trial_success_url=(
            args.trial_success_url
            or f"{base_url}/subscription/pending?expect_tier=trial"
        ),
        trial_cancel_url=(
            args.trial_cancel_url
            or f"{base_url}/plan?plan_cancelled=true&plan_tier=trial"
        ),
        trial_plan_id=args.trial_plan_id,
        trial_check_session_field=env_int("WINDSURF_TRIAL_CHECK_SESSION_FIELD", 1),
        trial_eligible_field=env_int("WINDSURF_TRIAL_ELIGIBLE_FIELD", 1),
        trial_sub_session_field=env_int("WINDSURF_TRIAL_SESSION_FIELD", 1),
        trial_sub_success_field=env_int("WINDSURF_TRIAL_SUCCESS_FIELD", 2),
        trial_sub_cancel_field=env_int("WINDSURF_TRIAL_CANCEL_FIELD", 3),
        trial_sub_turnstile_field=env_int("WINDSURF_TRIAL_TURNSTILE_FIELD", 4),
        trial_sub_plan_id_field=env_int("WINDSURF_TRIAL_PLAN_ID_FIELD", 5),
    )


def write_output(path: str, result: dict[str, Any], include_secrets: bool) -> None:
    summary = summarize_result(result, include_secrets=include_secrets)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    print_success(f"结果已写入 {path}")


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    config = build_config(args)

    try:
        if args.mode == "upload":
            result = upload_only_workflow(config, args)
        elif args.mode == "trial":
            result = trial_workflow(config, args)
        elif args.mode == "trial-browser":
            result = trial_browser_workflow(config, args)
        else:
            result = full_workflow(config, args)
        print_final_summary(result, show_secrets=args.show_secrets)
        if args.output_json:
            write_output(
                args.output_json,
                result,
                include_secrets=args.include_secrets_in_output,
            )
        return 0
    except WorkflowError as exc:
        print_warn(str(exc))
        return 1
    except KeyboardInterrupt:
        print_warn("用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())

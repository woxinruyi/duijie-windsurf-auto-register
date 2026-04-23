#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from patchright.async_api import async_playwright

from windsurf_auth_replay import (
    WorkflowError,
    env_bool,
    env_int,
    env_str,
    load_dotenv,
    print_step,
    print_success,
    print_warn,
    resolve_browser_executable_path,
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


async def maybe_accept_cookies(page) -> bool:
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


def prompt_password(label: str) -> str:
    password = getpass.getpass(f"{label}: ").strip()
    if not password:
        raise WorkflowError("缺少密码")
    return password


def extract_checkout_url(text: str) -> str:
    match = re.search(r"https://checkout\.stripe\.com/[^\s\x00\"']+", text)
    return match.group(0) if match else ""


async def fill_first(page, selectors: tuple[str, ...], value: str, timeout_s: int = 15) -> str:
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


async def click_first(page, selectors: tuple[str, ...], timeout_s: int = 20) -> str:
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


async def maybe_get_local_storage(page, key: str) -> str:
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


async def login_in_browser(page, email: str, password: str, timeout_s: int) -> None:
    await maybe_accept_cookies(page)
    email_selector = await fill_first(page, EMAIL_INPUT_SELECTORS, email, timeout_s=timeout_s)
    print_success(f"邮箱已填入 ({email_selector})")

    try:
        password_selector = await fill_first(page, PASSWORD_INPUT_SELECTORS, password, timeout_s=3)
        print_success(f"密码已填入 ({password_selector})")
    except WorkflowError:
        next_selector = await click_first(page, NEXT_BUTTON_SELECTORS, timeout_s=8)
        print_success(f"已点击下一步 ({next_selector})")
        password_selector = await fill_first(page, PASSWORD_INPUT_SELECTORS, password, timeout_s=timeout_s)
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
        submit_selector = await click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout_s=timeout_s)
        print_success(f"已点击登录按钮 ({submit_selector})")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        session_token = await maybe_get_local_storage(page, "devin_session_token")
        auth1_token = await maybe_get_local_storage(page, "devin_auth1_token")
        account_id = await maybe_get_local_storage(page, "devin_account_id")
        org_id = await maybe_get_local_storage(page, "devin_primary_org_id")
        if session_token and auth1_token and (account_id or org_id or "account/login" not in page.url):
            print_success("浏览器登录完成，登录态已写入 localStorage")
            return
        if "checkout.stripe.com" in page.url:
            return
        await page.wait_for_timeout(500)
    raise WorkflowError("浏览器登录后没有检测到完整登录态")


async def click_turnstile_checkbox(page) -> bool:
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


async def wait_for_turnstile_token(page, timeout_s: int, capture: CheckoutCapture) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await maybe_accept_cookies(page)
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

        await click_turnstile_checkbox(page)
        await page.wait_for_timeout(1000)

    try:
        await page.screenshot(path="trial_browser_turnstile_timeout.png")
    except Exception:
        pass
    raise WorkflowError(
        "等待 Turnstile token 超时。已保存截图到 trial_browser_turnstile_timeout.png"
    )


def install_response_capture(page, capture: CheckoutCapture) -> None:
    async def handle_response(response) -> None:
        try:
            if "SubscribeToPlan" in response.url:
                text = await response.text()
                checkout_url = extract_checkout_url(text)
                if checkout_url:
                    capture.checkout_url = checkout_url
                elif text.strip():
                    capture.subscribe_errors.append(text.strip()[:500])
        except Exception as exc:
            capture.subscribe_errors.append(f"读取 SubscribeToPlan 响应失败: {exc}")

    def on_response(response) -> None:
        asyncio.create_task(handle_response(response))

    page.on("response", on_response)


async def wait_for_checkout(page, capture: CheckoutCapture, timeout_s: int) -> str:
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


async def try_click_trial_button(page, timeout_s: int) -> Optional[str]:
    try:
        return await click_first(page, TRIAL_BUTTON_SELECTORS, timeout_s=timeout_s)
    except WorkflowError:
        return None


async def run_browser_trial(args: argparse.Namespace) -> str:
    browser_path = resolve_browser_executable_path(args.browser_path)
    launch_kwargs = {
        "headless": args.headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if browser_path:
        launch_kwargs["executable_path"] = browser_path

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        page = await context.new_page()
        capture = CheckoutCapture()
        install_response_capture(page, capture)

        try:
            print_step(f"正在打开登录页: {args.login_url}")
            await page.goto(args.login_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            await login_in_browser(page, args.email, args.password, timeout_s=args.timeout)
            await page.wait_for_timeout(2000)

            print_step(f"正在进入 Trial 页面: {args.billing_url}")
            await page.goto(args.billing_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            await maybe_accept_cookies(page)
            await page.wait_for_timeout(1500)
            if "account/login" in page.url:
                raise WorkflowError("进入 Trial 页面后被重定向回登录页，说明浏览器登录态还没建立完整")

            initial_click = await try_click_trial_button(page, timeout_s=5)
            if initial_click:
                print_success(f"已预先点击 Trial 按钮 ({initial_click})")

            print_step("正在等待并点击 Turnstile")
            turnstile_token = await wait_for_turnstile_token(page, args.timeout, capture)
            print_success(f"Turnstile token 已获取: {turnstile_token[:24]}...")

            if capture.checkout_url:
                return capture.checkout_url

            print_step("正在点击 Trial / Subscribe 按钮")
            clicked_selector = await click_first(page, TRIAL_BUTTON_SELECTORS, timeout_s=args.timeout)
            print_success(f"已点击订阅按钮 ({clicked_selector})")

            checkout_url = await wait_for_checkout(page, capture, timeout_s=args.timeout)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="浏览器自动化 Trial 流程：自动登录、点击页面并捕获 Stripe Checkout URL。",
    )
    parser.add_argument("--email", default=env_str("WINDSURF_EMAIL"))
    parser.add_argument("--password", default=env_str("WINDSURF_PASSWORD"))
    parser.add_argument(
        "--base-url",
        default=env_str("WINDSURF_BASE_URL", "https://windsurf.com"),
    )
    parser.add_argument(
        "--login-url",
        default="",
        help="默认使用 {base_url}/account/login",
    )
    parser.add_argument(
        "--billing-url",
        default=env_str("WINDSURF_TURNSTILE_SITE_URL", ""),
        help="默认使用 {base_url}/billing/individual?plan=9",
    )
    parser.add_argument(
        "--browser-path",
        default=env_str("TURNSTILE_BROWSER_PATH"),
        help="浏览器可执行文件路径，默认自动尝试 Chrome/Chromium。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=env_int("TURNSTILE_TIMEOUT", 90),
        help="单阶段等待超时（秒）。",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="使用无头浏览器。默认有界面，便于观察点击过程。",
    )
    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    if not args.email:
        raise SystemExit("缺少 --email 或 WINDSURF_EMAIL")
    if not args.password:
        if sys.stdin.isatty():
            args.password = prompt_password("请输入登录密码")
        else:
            raise SystemExit("缺少 --password 或 WINDSURF_PASSWORD")

    args.login_url = args.login_url or f"{args.base_url.rstrip('/')}/account/login"
    args.billing_url = args.billing_url or f"{args.base_url.rstrip('/')}/billing/individual?plan=9"
    args.timeout = max(10, args.timeout)

    try:
        checkout_url = asyncio.run(run_browser_trial(args))
        print("\n=== 完成 ===")
        print(f"Stripe Checkout URL: {checkout_url}")
        return 0
    except WorkflowError as exc:
        print_warn(str(exc))
        return 1
    except KeyboardInterrupt:
        print_warn("用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())

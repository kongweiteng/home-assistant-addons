"""Bounded headless-browser client for the undocumented read-only ESLink page."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import time
from typing import Callable
from urllib.parse import quote, urlencode

from .config import AccountConfig, AppConfig


LOGGER = logging.getLogger(__name__)
UTILITY_ORIGIN = "http://utilityserve-mobile.eslink.cc"
USER_INFO_PATH = "/api/usmart/v1.0/iot/userInfoQuery"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PORTAL_SETTLE_SECONDS = 2.0
CHROMEDRIVER_BINARY = "/usr/bin/chromedriver"
DESKTOP_WECHAT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "MicroMessenger/3.9.0 WindowsWechat"
)


class GasClientError(RuntimeError):
    """An upstream failure that is safe to summarize by exception class."""


class AuthRequiredError(GasClientError):
    """The private ESLink browser session is absent or expired."""


class ContractError(GasClientError):
    """The upstream response no longer matches the observed read-only contract."""


@dataclass(frozen=True)
class FetchResult:
    account_id: str
    payload: dict | None = None
    error: Exception | None = None


class EslinkBrowserClient:
    """Launch Chromium only for a polling cycle and persist its private profile."""

    def __init__(
        self,
        config: AppConfig,
        profile_dir: str | Path,
        browser_binary: str,
        *,
        driver_factory: Callable | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.profile_dir = Path(profile_dir)
        self.browser_binary = browser_binary
        self._driver_factory = driver_factory
        self._clock = clock

    def fetch_accounts(self) -> tuple[FetchResult, ...]:
        driver = self._create_driver()
        results: list[FetchResult] = []
        try:
            for account in self.config.accounts:
                try:
                    payload = self._fetch_with_reauthentication(driver, account)
                    results.append(FetchResult(account.id, payload=payload))
                except Exception as error:
                    LOGGER.warning(
                        "Account %s (%s) refresh failed (%s)",
                        account.id,
                        _masked_user_no(account.user_no),
                        type(error).__name__,
                    )
                    results.append(FetchResult(account.id, error=error))
        finally:
            try:
                driver.quit()
            except Exception:
                LOGGER.warning("Browser shutdown failed")
        return tuple(results)

    def _fetch_with_reauthentication(self, driver, account: AccountConfig) -> dict:
        try:
            return self._fetch_account(driver, account)
        except AuthRequiredError:
            self._authenticate(driver)
            return self._fetch_account(driver, account)

    def _authenticate(self, driver) -> None:
        previous_session = _session_cookie_value(driver.get_cookies())
        driver.get(self.config.portal_url)
        deadline = self._clock() + self.config.page_timeout_s
        ready_since: float | None = None
        while self._clock() < deadline:
            current_session = _session_cookie_value(driver.get_cookies())
            if current_session and current_session != previous_session:
                try:
                    driver.execute_script(
                        "history.replaceState({}, document.title, '/');"
                    )
                except Exception:
                    pass
                return
            if current_session and _portal_is_ready(driver):
                now = self._clock()
                if ready_since is None:
                    ready_since = now
                elif now - ready_since >= PORTAL_SETTLE_SECONDS:
                    try:
                        driver.execute_script(
                            "history.replaceState({}, document.title, '/');"
                        )
                    except Exception:
                        pass
                    return
            else:
                ready_since = None
            time.sleep(0.25)
        raise AuthRequiredError("service_hall_session_unavailable")

    def _fetch_account(self, driver, account: AccountConfig) -> dict:
        driver.get(build_iot_url(account))
        deadline = self._clock() + self.config.page_timeout_s
        while self._clock() < deadline:
            title = str(getattr(driver, "title", "") or "")
            if "物联网表充值" in title:
                break
            time.sleep(0.25)
        else:
            raise AuthRequiredError("iot_page_unavailable")
        response = driver.execute_async_script(
            _FETCH_SCRIPT,
            USER_INFO_PATH,
            account.user_no,
            self.config.page_timeout_s * 1000,
            MAX_RESPONSE_BYTES,
        )
        return parse_fetch_response(response)

    def _create_driver(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._driver_factory is not None:
            return self._driver_factory()
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.binary_location = self.browser_binary
        options.page_load_strategy = "eager"
        for argument in chrome_arguments(self.profile_dir):
            options.add_argument(argument)
        service = Service(
            executable_path=CHROMEDRIVER_BINARY,
            log_output=os.devnull,
        )
        driver = webdriver.Chrome(options=options, service=service)
        driver.set_page_load_timeout(self.config.page_timeout_s)
        driver.set_script_timeout(self.config.page_timeout_s + 5)
        try:
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd(
                "Network.setBlockedURLs", {"urls": list(blocked_third_party_urls())}
            )
        except Exception:
            LOGGER.warning("Third-party request blocking could not be enabled")
        return driver


def build_iot_url(account: AccountConfig) -> str:
    query = urlencode(
        {
            "billType": "null",
            "userName": account.user_name,
            "userNo": account.user_no,
        },
        quote_via=quote,
    )
    return f"{UTILITY_ORIGIN}/eslink/pay/iotPay?{query}"


def parse_fetch_response(response) -> dict:
    if not isinstance(response, dict):
        raise ContractError("browser_response_not_object")
    status = response.get("status")
    text = response.get("text")
    truncated = response.get("truncated")
    if status != 200 or not isinstance(text, str) or truncated is True:
        raise ContractError("bounded_http_response_failed")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ContractError("upstream_json_invalid") from error
    if not isinstance(payload, dict):
        raise ContractError("upstream_payload_not_object")
    if payload.get("success") is not True:
        if payload.get("echoCode") in {"910000", "910001"}:
            raise AuthRequiredError("upstream_session_rejected")
        raise GasClientError("upstream_query_rejected")
    return payload


def chrome_arguments(profile_dir: Path) -> tuple[str, ...]:
    return (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-port=0",
        f"--user-agent={DESKTOP_WECHAT_USER_AGENT}",
        f"--user-data-dir={profile_dir}",
    )


def blocked_third_party_urls() -> tuple[str, ...]:
    return (
        "*://webapi.amap.com/*",
        "*://restapi.amap.com/*",
        "*://map.qq.com/*",
        "*://mapapi.qq.com/*",
        "*://s4.cnzz.com/*",
        "*://sentry.eslink.com/*",
        "*://at.alicdn.com/*",
    )


def _masked_user_no(value: str) -> str:
    return "*" * max(0, len(value) - 4) + value[-4:]


def _session_cookie_value(cookies) -> str | None:
    for item in cookies:
        if item.get("name") == "SESSION" and item.get("value"):
            return str(item["value"])
    return None


def _portal_is_ready(driver) -> bool:
    title = str(getattr(driver, "title", "") or "")
    if "微信服务大厅" not in title:
        return False
    try:
        body_text = driver.execute_script(
            "return document.body ? document.body.innerText : '';"
        )
    except Exception:
        return False
    return isinstance(body_text, str) and (
        "物联表缴费" in body_text or "物联表使用" in body_text
    )


_FETCH_SCRIPT = r"""
const done = arguments[arguments.length - 1];
const path = arguments[0];
const userNo = arguments[1];
const timeoutMs = arguments[2];
const maxBytes = arguments[3];
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), timeoutMs);
fetch(path, {
  method: "POST",
  credentials: "same-origin",
  headers: {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest"
  },
  body: new URLSearchParams({meterNo: userNo}).toString(),
  signal: controller.signal
}).then(async response => {
  const text = await response.text();
  clearTimeout(timer);
  done({
    status: response.status,
    text: text.slice(0, maxBytes),
    truncated: text.length > maxBytes
  });
}).catch(error => {
  clearTimeout(timer);
  done({status: 0, text: "", truncated: false, error: error.name});
});
"""

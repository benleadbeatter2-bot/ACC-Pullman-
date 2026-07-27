import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


ACCOR_URL = os.environ.get(
    "ACCOR_URL",
    "https://all.accor.com/hotel/B436/index.en.shtml",
)

TARGET_PRICE = float(os.environ.get("TARGET_PRICE", "0"))
CURRENCY = os.environ.get("CURRENCY", "AUD").upper()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SCREENSHOT_PATH = Path("accor-price-check.png")
RESULT_PATH = Path("price-result.json")


def normalise_amount(value: str) -> float | None:
    """
    Convert values such as:
      6,450
      6 450
      245.50
      1.245,50

    into a Python float.
    """
    cleaned = re.sub(r"[^\d,.]", "", value).strip()

    if not cleaned:
        return None

    # European-style number: 1.245,50
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    elif "," in cleaned:
        parts = cleaned.split(",")

        # 6,450 is probably a thousands separator.
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")

    try:
        amount = float(cleaned)
    except ValueError:
        return None

    # Discard obviously irrelevant tiny or enormous values.
    if amount < 20 or amount > 1_000_000:
        return None

    return amount


def price_patterns(currency: str) -> list[str]:
    patterns = {
        "AUD": [
            r"(?:A\$|AU\$|AUD)\s*([\d][\d\s,.]*)",
            r"([\d][\d\s,.]*)\s*(?:AUD)",
        ],
        "THB": [
            r"(?:THB|฿)\s*([\d][\d\s,.]*)",
            r"([\d][\d\s,.]*)\s*(?:THB|บาท)",
        ],
        "USD": [
            r"(?:US\$|USD)\s*([\d][\d\s,.]*)",
            r"([\d][\d\s,.]*)\s*(?:USD)",
        ],
        "GBP": [
            r"(?:£|GBP)\s*([\d][\d\s,.]*)",
            r"([\d][\d\s,.]*)\s*(?:GBP)",
        ],
        "EUR": [
            r"(?:€|EUR)\s*([\d][\d\s,.]*)",
            r"([\d][\d\s,.]*)\s*(?:EUR)",
        ],
    }

    return patterns.get(
        currency,
        [
            rf"{re.escape(currency)}\s*([\d][\d\s,.]*)",
            rf"([\d][\d\s,.]*)\s*{re.escape(currency)}",
        ],
    )


def extract_prices(page_text: str, currency: str) -> list[float]:
    prices: list[float] = []

    for pattern in price_patterns(currency):
        matches = re.findall(pattern, page_text, flags=re.IGNORECASE)

        for match in matches:
            amount = normalise_amount(match)

            if amount is not None:
                prices.append(amount)

    return sorted(set(prices))


def click_cookie_button(page) -> None:
    possible_buttons = [
        "Accept all",
        "Accept All",
        "Accept cookies",
        "Allow all",
        "I accept",
        "Agree",
        "Continue without accepting",
    ]

    for button_text in possible_buttons:
        try:
            button = page.get_by_role(
                "button",
                name=re.compile(
                    rf"^{re.escape(button_text)}$",
                    re.IGNORECASE,
                ),
            )

            if button.count() > 0 and button.first.is_visible():
                button.first.click(timeout=3_000)
                page.wait_for_timeout(1_000)
                return
        except Exception:
            continue


def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram is not configured.")
        return False

    endpoint = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        endpoint,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )

    response.raise_for_status()
    return True


def write_github_summary(
    lowest_price: float | None,
    prices: list[float],
    status: str,
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")

    if not summary_path:
        return

    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write("# Pullman Khao Lak price check\n\n")
        summary.write(f"**Status:** {status}\n\n")
        summary.write(f"**Currency:** {CURRENCY}\n\n")

        if lowest_price is not None:
            summary.write(
                f"**Lowest detected price:** "
                f"{CURRENCY} {lowest_price:,.2f}\n\n"
            )

        if TARGET_PRICE > 0:
            summary.write(
                f"**Target price:** "
                f"{CURRENCY} {TARGET_PRICE:,.2f}\n\n"
            )

        summary.write(
            f"**Prices detected:** {len(prices)}\n\n"
        )
        summary.write(f"[Open Accor search]({ACCOR_URL})\n")


def main() -> int:
    print("Checking Pullman Khao Lak Resort")
    print(f"URL: {ACCOR_URL}")
    print(f"Currency: {CURRENCY}")

    if TARGET_PRICE > 0:
        print(f"Target price: {CURRENCY} {TARGET_PRICE:,.2f}")
    else:
        print("No target price configured.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            locale="en-AU",
            timezone_id="Australia/Perth",
            viewport={"width": 1440, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        try:
            page.goto(
                ACCOR_URL,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            click_cookie_button(page)

            # Give Accor's JavaScript time to load room rates.
            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=30_000,
                )
            except PlaywrightTimeoutError:
                print(
                    "Network did not become completely idle; "
                    "continuing."
                )

            page.wait_for_timeout(10_000)

            page.screenshot(
                path=str(SCREENSHOT_PATH),
                full_page=True,
            )

            body_text = page.locator("body").inner_text(
                timeout=30_000
            )

        except Exception as error:
            print(f"Unable to load Accor page: {error}")
            browser.close()
            write_github_summary(
                lowest_price=None,
                prices=[],
                status="Page check failed",
            )
            return 1

        finally:
            if browser.is_connected():
                browser.close()

    prices = extract_prices(body_text, CURRENCY)

    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "hotel": "Pullman Khao Lak Resort",
        "hotel_code": "B436",
        "currency": CURRENCY,
        "target_price": TARGET_PRICE,
        "prices_detected": prices,
        "lowest_price": prices[0] if prices else None,
        "url": ACCOR_URL,
    }

    RESULT_PATH.write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )

    if not prices:
        message = (
            "Pullman Khao Lak price check completed, but no "
            f"{CURRENCY} prices were detected.\n\n"
            "Check the screenshot in the GitHub Actions run. "
            "The Accor page may have changed, the room may be "
            "unavailable, or the search URL may have expired."
        )

        print(message)
        write_github_summary(
            lowest_price=None,
            prices=[],
            status="No prices detected",
        )

        try:
            send_telegram(message)
        except Exception as error:
            print(f"Telegram notification failed: {error}")

        return 1

    lowest_price = prices[0]

    print(f"Detected prices: {prices}")
    print(
        f"Lowest price: {CURRENCY} {lowest_price:,.2f}"
    )

    if TARGET_PRICE > 0 and lowest_price <= TARGET_PRICE:
        status = "Target price reached"

        message = (
            "🏨 PULLMAN KHAO LAK PRICE ALERT\n\n"
            f"Lowest detected price: "
            f"{CURRENCY} {lowest_price:,.2f}\n"
            f"Your target: {CURRENCY} {TARGET_PRICE:,.2f}\n\n"
            f"Book/check the rate:\n{ACCOR_URL}"
        )

        print(message)

        try:
            send_telegram(message)
        except Exception as error:
            print(f"Telegram notification failed: {error}")

    elif TARGET_PRICE > 0:
        status = "Price remains above target"

        print(
            f"Price is {CURRENCY} "
            f"{lowest_price - TARGET_PRICE:,.2f} "
            "above the target."
        )

    else:
        status = "Price check completed"

        message = (
            "🏨 Pullman Khao Lak price check\n\n"
            f"Lowest detected price: "
            f"{CURRENCY} {lowest_price:,.2f}\n\n"
            f"{ACCOR_URL}"
        )

        # Without a target, send the result on every run.
        try:
            send_telegram(message)
        except Exception as error:
            print(f"Telegram notification failed: {error}")

    write_github_summary(
        lowest_price=lowest_price,
        prices=prices,
        status=status,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


# ============================================================
# HOTEL SETTINGS
# ============================================================

HOTEL_NAME = "Pullman Khao Lak Resort"
HOTEL_CODE = "B436"

DEFAULT_ACCOR_URL = (
    "https://all.accor.com/hotel/B436/index.en.shtml"
)

EMAIL_TO = "benleadbeatter2@hotmail.com"

SCREENSHOT_FILE = Path("accor-price-check.png")
RESULT_FILE = Path("price-result.json")


# ============================================================
# ENVIRONMENT VARIABLE HELPERS
# ============================================================

def environment_value(name: str, default: str) -> str:
    """
    Return an environment variable.

    If the variable is missing or blank, return the default.
    This prevents blank GitHub variables from overriding defaults.
    """
    value = os.environ.get(name, "").strip()

    if value:
        return value

    return default


ACCOR_URL = environment_value(
    "ACCOR_URL",
    DEFAULT_ACCOR_URL,
)

CURRENCY = environment_value(
    "CURRENCY",
    "AUD",
).upper()

EMAIL_MODE = environment_value(
    "EMAIL_MODE",
    "always",
).lower()

EMAIL_FROM = environment_value(
    "EMAIL_FROM",
    "",
)

EMAIL_PASSWORD = environment_value(
    "EMAIL_PASSWORD",
    "",
)

SMTP_HOST = environment_value(
    "SMTP_HOST",
    "smtp-mail.outlook.com",
)

try:
    SMTP_PORT = int(
        environment_value("SMTP_PORT", "587")
    )
except ValueError:
    SMTP_PORT = 587

try:
    TARGET_PRICE = float(
        environment_value("TARGET_PRICE", "0")
    )
except ValueError:
    TARGET_PRICE = 0.0


# ============================================================
# PRICE EXTRACTION
# ============================================================

def normalise_price(raw_value: str) -> Optional[float]:
    """
    Convert text such as:
        A$245
        AUD 245.50
        THB 6,450
        1.245,50 EUR

    into a float.
    """
    cleaned = re.sub(
        r"[^\d,.]",
        "",
        raw_value,
    ).strip()

    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        # Example: 1.245,50
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "")
            cleaned = cleaned.replace(",", ".")

        # Example: 1,245.50
        else:
            cleaned = cleaned.replace(",", "")

    elif "," in cleaned:
        sections = cleaned.split(",")

        # Example: 6,450
        if len(sections[-1]) == 3:
            cleaned = cleaned.replace(",", "")

        # Example: 245,50
        else:
            cleaned = cleaned.replace(",", ".")

    try:
        amount = float(cleaned)
    except ValueError:
        return None

    # Ignore numbers that are unlikely to be hotel prices.
    if amount < 20 or amount > 1_000_000:
        return None

    return amount


def currency_patterns(currency: str) -> list[str]:
    known_patterns = {
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

    if currency in known_patterns:
        return known_patterns[currency]

    escaped_currency = re.escape(currency)

    return [
        rf"{escaped_currency}\s*([\d][\d\s,.]*)",
        rf"([\d][\d\s,.]*)\s*{escaped_currency}",
    ]


def extract_prices(
    page_text: str,
    currency: str,
) -> list[float]:
    prices: list[float] = []

    for pattern in currency_patterns(currency):
        matches = re.findall(
            pattern,
            page_text,
            flags=re.IGNORECASE,
        )

        for match in matches:
            price = normalise_price(match)

            if price is not None:
                prices.append(price)

    return sorted(set(prices))


# ============================================================
# PAGE HANDLING
# ============================================================

def dismiss_cookie_banner(page) -> None:
    possible_button_names = [
        "Accept all",
        "Accept All",
        "Accept cookies",
        "Allow all",
        "I accept",
        "Agree",
        "Continue without accepting",
    ]

    for button_name in possible_button_names:
        try:
            button = page.get_by_role(
                "button",
                name=re.compile(
                    rf"^{re.escape(button_name)}$",
                    re.IGNORECASE,
                ),
            )

            if (
                button.count() > 0
                and button.first.is_visible()
            ):
                button.first.click(timeout=3_000)
                page.wait_for_timeout(1_000)
                return

        except Exception:
            continue


def check_accor_page() -> tuple[list[float], str]:
    print(f"Opening Accor page: {ACCOR_URL}")

    if not ACCOR_URL.startswith(
        ("https://", "http://")
    ):
        raise ValueError(
            "ACCOR_URL is not a valid web address. "
            "It must start with https:// or http://"
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                (
                    "--disable-blink-features="
                    "AutomationControlled"
                ),
            ],
        )

        context = browser.new_context(
            locale="en-AU",
            timezone_id="Australia/Perth",
            viewport={
                "width": 1440,
                "height": 1800,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        try:
            response = page.goto(
                ACCOR_URL,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            if response is not None:
                print(
                    f"HTTP response: {response.status}"
                )

            dismiss_cookie_banner(page)

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=30_000,
                )

            except PlaywrightTimeoutError:
                print(
                    "The page remained active. "
                    "Continuing with the check."
                )

            # Allow dynamic prices time to render.
            page.wait_for_timeout(12_000)

            page.screenshot(
                path=str(SCREENSHOT_FILE),
                full_page=True,
            )

            page_title = page.title()

            page_text = page.locator(
                "body"
            ).inner_text(timeout=30_000)

            print(f"Page title: {page_title}")

            prices = extract_prices(
                page_text,
                CURRENCY,
            )

            return prices, page_title

        finally:
            context.close()
            browser.close()


# ============================================================
# EMAIL
# ============================================================

def send_email(
    subject: str,
    body: str,
    screenshot: Optional[Path] = None,
) -> None:
    if not EMAIL_FROM:
        raise RuntimeError(
            "EMAIL_FROM secret is missing."
        )

    if not EMAIL_PASSWORD:
        raise RuntimeError(
            "EMAIL_PASSWORD secret is missing."
        )

    message = EmailMessage()

    message["Subject"] = subject
    message["From"] = EMAIL_FROM
    message["To"] = EMAIL_TO

    message.set_content(body)

    if screenshot and screenshot.exists():
        image_data = screenshot.read_bytes()

        message.add_attachment(
            image_data,
            maintype="image",
            subtype="png",
            filename=screenshot.name,
        )

    print(
        f"Connecting to {SMTP_HOST}:{SMTP_PORT}"
    )

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT,
        timeout=60,
    ) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()

        smtp.login(
            EMAIL_FROM,
            EMAIL_PASSWORD,
        )

        smtp.send_message(message)

    print(f"Email sent to {EMAIL_TO}")


# ============================================================
# GITHUB SUMMARY AND RESULT FILE
# ============================================================

def write_result_file(
    successful: bool,
    status: str,
    prices: Optional[list[float]] = None,
    page_title: str = "",
    error: str = "",
) -> None:
    prices = prices or []

    result = {
        "checked_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "hotel": HOTEL_NAME,
        "hotel_code": HOTEL_CODE,
        "successful": successful,
        "status": status,
        "currency": CURRENCY,
        "target_price": TARGET_PRICE,
        "lowest_detected_price": (
            prices[0] if prices else None
        ),
        "prices_detected": prices,
        "page_title": page_title,
        "email_recipient": EMAIL_TO,
        "url": ACCOR_URL,
        "error": error or None,
    }

    RESULT_FILE.write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )


def write_github_summary(
    status: str,
    prices: Optional[list[float]] = None,
) -> None:
    summary_path = os.environ.get(
        "GITHUB_STEP_SUMMARY",
        "",
    )

    if not summary_path:
        return

    prices = prices or []

    lines = [
        "# Pullman Khao Lak price check",
        "",
        f"**Status:** {status}",
        "",
        f"**Currency:** {CURRENCY}",
        "",
        f"**Email recipient:** {EMAIL_TO}",
        "",
    ]

    if prices:
        lines.extend(
            [
                (
                    "**Lowest detected price:** "
                    f"{CURRENCY} {prices[0]:,.2f}"
                ),
                "",
                (
                    "**Prices detected:** "
                    + ", ".join(
                        f"{CURRENCY} {price:,.2f}"
                        for price in prices[:10]
                    )
                ),
                "",
            ]
        )

    if TARGET_PRICE > 0:
        lines.extend(
            [
                (
                    "**Target price:** "
                    f"{CURRENCY} {TARGET_PRICE:,.2f}"
                ),
                "",
            ]
        )

    lines.extend(
        [
            f"[Open Accor booking page]({ACCOR_URL})",
            "",
        ]
    )

    with open(
        summary_path,
        "a",
        encoding="utf-8",
    ) as summary:
        summary.write("\n".join(lines))


# ============================================================
# EMAIL CONTENT
# ============================================================

def create_price_email(
    prices: list[float],
) -> tuple[str, str]:
    lowest_price = prices[0]

    target_reached = (
        TARGET_PRICE > 0
        and lowest_price <= TARGET_PRICE
    )

    if target_reached:
        subject = (
            "Pullman Khao Lak price alert – "
            f"{CURRENCY} {lowest_price:,.2f}"
        )
    else:
        subject = (
            "Pullman Khao Lak price check – "
            f"{CURRENCY} {lowest_price:,.2f}"
        )

    body_lines = [
        "Pullman Khao Lak Resort price check",
        "",
        (
            "Lowest price detected on the page: "
            f"{CURRENCY} {lowest_price:,.2f}"
        ),
    ]

    if TARGET_PRICE > 0:
        body_lines.extend(
            [
                (
                    "Your target price: "
                    f"{CURRENCY} {TARGET_PRICE:,.2f}"
                ),
                (
                    "Target reached: "
                    f"{'YES' if target_reached else 'NO'}"
                ),
            ]
        )

    body_lines.extend(
        [
            "",
            "Possible prices detected:",
        ]
    )

    for price in prices[:10]:
        body_lines.append(
            f"- {CURRENCY} {price:,.2f}"
        )

    body_lines.extend(
        [
            "",
            "Check the current Accor rate here:",
            ACCOR_URL,
            "",
            (
                "The Accor page screenshot from this "
                "automated check is attached."
            ),
            "",
            (
                "Check the room type, number of nights, "
                "taxes, meals, member rate and cancellation "
                "conditions before booking."
            ),
        ]
    )

    return subject, "\n".join(body_lines)


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    print(f"Hotel: {HOTEL_NAME}")
    print(f"Accor URL: {ACCOR_URL}")
    print(f"Currency: {CURRENCY}")
    print(f"Target price: {TARGET_PRICE}")
    print(f"Email mode: {EMAIL_MODE}")
    print(f"Email recipient: {EMAIL_TO}")

    try:
        prices, page_title = check_accor_page()

    except Exception as error:
        error_text = (
            f"{type(error).__name__}: {error}"
        )

        print(
            f"Accor price check failed: {error_text}"
        )

        write_result_file(
            successful=False,
            status="Page check failed",
            error=error_text,
        )

        write_github_summary(
            status="Page check failed",
        )

        try:
            send_email(
                subject=(
                    "Pullman Khao Lak price check failed"
                ),
                body=(
                    "The Pullman Khao Lak automated price "
                    "check failed.\n\n"
                    f"Error:\n{error_text}\n\n"
                    f"Accor URL:\n{ACCOR_URL}\n\n"
                    "Open the GitHub Actions run for the "
                    "complete log."
                ),
                screenshot=SCREENSHOT_FILE,
            )

        except Exception as email_error:
            print(
                "Failure email could not be sent: "
                f"{email_error}"
            )

        return 1

    if not prices:
        status = (
            f"No {CURRENCY} prices were detected"
        )

        print(status)

        write_result_file(
            successful=False,
            status=status,
            page_title=page_title,
        )

        write_github_summary(
            status=status,
        )

        try:
            send_email(
                subject=(
                    "Pullman Khao Lak – "
                    "no price detected"
                ),
                body=(
                    "The Accor page opened successfully, "
                    f"but no {CURRENCY} room prices were "
                    "detected.\n\n"
                    "Possible reasons:\n"
                    "- The URL does not contain booking dates.\n"
                    "- The hotel has no availability.\n"
                    "- Accor displayed another currency.\n"
                    "- Accor changed the page layout.\n"
                    "- Accor blocked the automated browser.\n\n"
                    f"Page title:\n{page_title}\n\n"
                    f"Accor URL:\n{ACCOR_URL}\n\n"
                    "The screenshot is attached."
                ),
                screenshot=SCREENSHOT_FILE,
            )

        except Exception as email_error:
            print(
                "Email could not be sent: "
                f"{email_error}"
            )

        return 1

    lowest_price = prices[0]

    print(
        "Lowest detected price: "
        f"{CURRENCY} {lowest_price:,.2f}"
    )

    write_result_file(
        successful=True,
        status="Price detected",
        prices=prices,
        page_title=page_title,
    )

    should_email = True

    if EMAIL_MODE == "target":
        should_email = (
            TARGET_PRICE > 0
            and lowest_price <= TARGET_PRICE
        )

    if should_email:
        subject, body = create_price_email(prices)

        try:
            send_email(
                subject=subject,
                body=body,
                screenshot=SCREENSHOT_FILE,
            )

        except Exception as error:
            print(
                "Price was detected, but the email failed: "
                f"{error}"
            )

            write_github_summary(
                status="Price detected, but email failed",
                prices=prices,
            )

            return 1

        final_status = "Price detected and email sent"

    else:
        print(
            "Price is above the target. "
            "No email was required."
        )

        final_status = (
            "Price detected; target not reached"
        )

    write_github_summary(
        status=final_status,
        prices=prices,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

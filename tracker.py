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
# HOTEL DETAILS
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
# ENVIRONMENT VARIABLES
# ============================================================

def get_environment_value(
    name: str,
    default: str,
) -> str:
    """
    Return a GitHub environment variable.

    If it is missing or blank, return the default value.
    """
    value = os.environ.get(name, "").strip()

    if value:
        return value

    return default


ACCOR_URL = get_environment_value(
    "ACCOR_URL",
    DEFAULT_ACCOR_URL,
)

CURRENCY = get_environment_value(
    "CURRENCY",
    "THB",
).upper()

EMAIL_FROM = get_environment_value(
    "EMAIL_FROM",
    "",
)

EMAIL_PASSWORD = get_environment_value(
    "EMAIL_PASSWORD",
    "",
)

SMTP_HOST = get_environment_value(
    "SMTP_HOST",
    "smtp-mail.outlook.com",
)

try:
    SMTP_PORT = int(
        get_environment_value(
            "SMTP_PORT",
            "587",
        )
    )
except ValueError:
    SMTP_PORT = 587

try:
    MIN_PRICE = float(
        get_environment_value(
            "MIN_PRICE",
            "1300",
        )
    )
except ValueError:
    MIN_PRICE = 1300.0

try:
    MAX_PRICE = float(
        get_environment_value(
            "MAX_PRICE",
            "1400",
        )
    )
except ValueError:
    MAX_PRICE = 1400.0


# ============================================================
# PRICE PARSING
# ============================================================

def normalise_price(
    raw_value: str,
) -> Optional[float]:
    """
    Convert displayed price text into a float.

    Examples:
        THB 1,350
        ฿1,399
        AUD 245.50
        1.245,50 EUR
    """
    cleaned = re.sub(
        r"[^\d,.]",
        "",
        raw_value,
    ).strip()

    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        # European format: 1.245,50
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "")
            cleaned = cleaned.replace(",", ".")

        # English format: 1,245.50
        else:
            cleaned = cleaned.replace(",", "")

    elif "," in cleaned:
        sections = cleaned.split(",")

        # Thousands separator: 1,350
        if len(sections[-1]) == 3:
            cleaned = cleaned.replace(",", "")

        # Decimal separator: 245,50
        else:
            cleaned = cleaned.replace(",", ".")

    try:
        amount = float(cleaned)
    except ValueError:
        return None

    # Exclude values unlikely to be room prices.
    if amount < 20 or amount > 1_000_000:
        return None

    return amount


def get_currency_patterns(
    currency: str,
) -> list[str]:
    patterns = {
        "THB": [
            r"(?:THB|฿)\s*([\d][\d\s,.]*)",
            r"([\d][\d\s,.]*)\s*(?:THB|บาท)",
        ],
        "AUD": [
            r"(?:A\$|AU\$|AUD)\s*([\d][\d\s,.]*)",
            r"([\d][\d\s,.]*)\s*(?:AUD)",
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

    if currency in patterns:
        return patterns[currency]

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

    for pattern in get_currency_patterns(currency):
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
# ACCOR PAGE HANDLING
# ============================================================

def dismiss_cookie_banner(page) -> None:
    possible_buttons = [
        "Accept all",
        "Accept All",
        "Accept cookies",
        "Allow all",
        "I accept",
        "Agree",
        "Continue without accepting",
    ]

    for button_name in possible_buttons:
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
                button.first.click(
                    timeout=3_000
                )

                page.wait_for_timeout(1_000)
                return

        except Exception:
            continue


def check_accor_prices() -> tuple[list[float], str]:
    if not ACCOR_URL.startswith(
        ("https://", "http://")
    ):
        raise ValueError(
            "ACCOR_URL must start with "
            "https:// or http://"
        )

    print(f"Opening Accor URL: {ACCOR_URL}")

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
                    "Page remained active. "
                    "Continuing with the price check."
                )

            # Allow dynamic room prices time to appear.
            page.wait_for_timeout(15_000)

            page.screenshot(
                path=str(SCREENSHOT_FILE),
                full_page=True,
            )

            page_title = page.title()

            page_text = page.locator(
                "body"
            ).inner_text(
                timeout=30_000
            )

            prices = extract_prices(
                page_text,
                CURRENCY,
            )

            print(f"Page title: {page_title}")
            print(f"Detected prices: {prices}")

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
            "EMAIL_FROM GitHub secret is missing."
        )

    if not EMAIL_PASSWORD:
        raise RuntimeError(
            "EMAIL_PASSWORD GitHub secret is missing."
        )

    message = EmailMessage()

    message["Subject"] = subject
    message["From"] = EMAIL_FROM
    message["To"] = EMAIL_TO

    message.set_content(body)

    if screenshot and screenshot.exists():
        screenshot_data = screenshot.read_bytes()

        message.add_attachment(
            screenshot_data,
            maintype="image",
            subtype="png",
            filename=screenshot.name,
        )

    print(
        f"Connecting to email server "
        f"{SMTP_HOST}:{SMTP_PORT}"
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
# RESULT FILE
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
        "minimum_price": MIN_PRICE,
        "maximum_price": MAX_PRICE,
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
        json.dumps(
            result,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# GITHUB ACTIONS SUMMARY
# ============================================================

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
        (
            f"**Desired range:** "
            f"{CURRENCY} {MIN_PRICE:,.2f} to "
            f"{CURRENCY} {MAX_PRICE:,.2f}"
        ),
        "",
        f"**Email recipient:** {EMAIL_TO}",
        "",
    ]

    if prices:
        lowest_price = prices[0]

        lines.extend(
            [
                (
                    "**Lowest detected price:** "
                    f"{CURRENCY} "
                    f"{lowest_price:,.2f}"
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

def create_range_email(
    lowest_price: float,
    prices: list[float],
) -> tuple[str, str]:
    subject = (
        "Pullman Khao Lak price in range – "
        f"{CURRENCY} {lowest_price:,.2f}"
    )

    body_lines = [
        "Pullman Khao Lak Resort price alert",
        "",
        (
            "The lowest detected price is within "
            "your requested range."
        ),
        "",
        (
            "Lowest detected price: "
            f"{CURRENCY} {lowest_price:,.2f}"
        ),
        (
            "Requested range: "
            f"{CURRENCY} {MIN_PRICE:,.2f} to "
            f"{CURRENCY} {MAX_PRICE:,.2f}"
        ),
        "",
        "Possible prices detected:",
    ]

    for price in prices[:10]:
        body_lines.append(
            f"- {CURRENCY} {price:,.2f}"
        )

    body_lines.extend(
        [
            "",
            "Check the current price here:",
            ACCOR_URL,
            "",
            (
                "A screenshot of the Accor page "
                "is attached."
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


def create_no_price_email(
    page_title: str,
) -> tuple[str, str]:
    subject = (
        "Pullman Khao Lak – no price detected"
    )

    body = (
        "The Accor page opened, but the tracker "
        f"could not detect any {CURRENCY} prices.\n\n"
        "Possible reasons:\n"
        "- The booking URL does not include dates.\n"
        "- The hotel has no availability.\n"
        "- Accor displayed a different currency.\n"
        "- Accor changed the booking page.\n"
        "- The automated browser was blocked.\n\n"
        f"Page title:\n{page_title}\n\n"
        f"Accor URL:\n{ACCOR_URL}\n\n"
        "A screenshot is attached."
    )

    return subject, body


# ============================================================
# MAIN PROGRAM
# ============================================================

def main() -> int:
    print(f"Hotel: {HOTEL_NAME}")
    print(f"Currency: {CURRENCY}")

    print(
        "Desired range: "
        f"{CURRENCY} {MIN_PRICE:,.2f} to "
        f"{CURRENCY} {MAX_PRICE:,.2f}"
    )

    print(f"Email recipient: {EMAIL_TO}")

    if MIN_PRICE > MAX_PRICE:
        print(
            "Configuration error: MIN_PRICE is "
            "higher than MAX_PRICE."
        )

        write_result_file(
            successful=False,
            status="Invalid price range",
            error=(
                "MIN_PRICE cannot be higher "
                "than MAX_PRICE."
            ),
        )

        return 1

    try:
        prices, page_title = check_accor_prices()

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
                    "The automated Pullman Khao Lak "
                    "price check failed.\n\n"
                    f"Error:\n{error_text}\n\n"
                    f"Accor URL:\n{ACCOR_URL}\n\n"
                    "Open GitHub Actions for the full log."
                ),
                screenshot=SCREENSHOT_FILE,
            )

        except Exception as email_error:
            print(
                "Failure email could not be sent: "
                f"{type(email_error).__name__}: "
                f"{email_error}"
            )

        return 1

    if not prices:
        status = (
            f"No {CURRENCY} prices detected"
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
            subject, body = create_no_price_email(
                page_title
            )

            send_email(
                subject=subject,
                body=body,
                screenshot=SCREENSHOT_FILE,
            )

        except Exception as email_error:
            print(
                "No-price email could not be sent: "
                f"{type(email_error).__name__}: "
                f"{email_error}"
            )

        # Mark the workflow successful because the
        # tracker itself completed its check.
        return 0

    lowest_price = prices[0]

    price_in_range = (
        MIN_PRICE
        <= lowest_price
        <= MAX_PRICE
    )

    if price_in_range:
        status = "Price is within requested range"

        print(
            "PRICE ALERT: "
            f"{CURRENCY} {lowest_price:,.2f} "
            "is within the requested range."
        )

        try:
            subject, body = create_range_email(
                lowest_price=lowest_price,
                prices=prices,
            )

            send_email(
                subject=subject,
                body=body,
                screenshot=SCREENSHOT_FILE,
            )

            status = (
                "Price is within range and email sent"
            )

        except Exception as email_error:
            print(
                "Price was in range, but email failed: "
                f"{type(email_error).__name__}: "
                f"{email_error}"
            )

            status = (
                "Price is within range, but email failed"
            )

    elif lowest_price < MIN_PRICE:
        status = (
            "Price is below requested range"
        )

        print(
            f"Lowest price {CURRENCY} "
            f"{lowest_price:,.2f} is below "
            f"{CURRENCY} {MIN_PRICE:,.2f}."
        )

        print("No email sent.")

    else:
        status = (
            "Price is above requested range"
        )

        print(
            f"Lowest price {CURRENCY} "
            f"{lowest_price:,.2f} is above "
            f"{CURRENCY} {MAX_PRICE:,.2f}."
        )

        print("No email sent.")

    write_result_file(
        successful=True,
        status=status,
        prices=prices,
        page_title=page_title,
    )

    write_github_summary(
        status=status,
        prices=prices,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


HOTEL_NAME = "Pullman Khao Lak Resort"
HOTEL_CODE = "B436"

DEFAULT_HOTEL_URL = (
    "https://all.accor.com/hotel/B436/index.en.shtml"
)

ACCOR_URL = os.environ.get(
    "ACCOR_URL",
    DEFAULT_HOTEL_URL,
).strip()

CURRENCY = os.environ.get(
    "CURRENCY",
    "AUD",
).strip().upper()

TARGET_PRICE = float(
    os.environ.get("TARGET_PRICE", "0") or "0"
)

# Email recipient requested by the user.
EMAIL_TO = os.environ.get(
    "EMAIL_TO",
    "benleadbeatter2@hotmail.com",
).strip()

EMAIL_FROM = os.environ.get(
    "EMAIL_FROM",
    "",
).strip()

EMAIL_PASSWORD = os.environ.get(
    "EMAIL_PASSWORD",
    "",
).strip()

# Outlook/Hotmail defaults.
SMTP_HOST = os.environ.get(
    "SMTP_HOST",
    "smtp-mail.outlook.com",
).strip()

SMTP_PORT = int(
    os.environ.get("SMTP_PORT", "587")
)

# always = email after every check
# target = email only when price is at or below the target
# change = email when the lowest price changes
EMAIL_MODE = os.environ.get(
    "EMAIL_MODE",
    "always",
).strip().lower()

SCREENSHOT_PATH = Path("accor-price-check.png")
RESULT_PATH = Path("price-result.json")
PREVIOUS_PRICE_PATH = Path("previous-price.json")


def normalise_amount(value: str) -> float | None:
    """
    Convert displayed prices into floats.

    Examples:
        245.50
        6,450
        6 450
        1.245,50
    """
    cleaned = re.sub(r"[^\d,.]", "", value).strip()

    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            # European style: 1.245,50
            cleaned = cleaned.replace(".", "")
            cleaned = cleaned.replace(",", ".")
        else:
            # Australian/English style: 1,245.50
            cleaned = cleaned.replace(",", "")

    elif "," in cleaned:
        sections = cleaned.split(",")

        if len(sections[-1]) == 3:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")

    try:
        amount = float(cleaned)
    except ValueError:
        return None

    # Remove numbers unlikely to be room prices.
    if amount < 20 or amount > 1_000_000:
        return None

    return amount


def get_currency_patterns(currency: str) -> list[str]:
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
        "EUR": [
            r"(?:€|EUR)\s*([\d][\d\s,.]*)",
            r"([\d][\d\s,.]*)\s*(?:EUR)",
        ],
        "GBP": [
            r"(?:£|GBP)\s*([\d][\d\s,.]*)",
            r"([\d][\d\s,.]*)\s*(?:GBP)",
        ],
    }

    return patterns.get(
        currency,
        [
            rf"{re.escape(currency)}\s*([\d][\d\s,.]*)",
            rf"([\d][\d\s,.]*)\s*{re.escape(currency)}",
        ],
    )


def extract_prices(
    page_text: str,
    currency: str,
) -> list[float]:
    detected_prices: list[float] = []

    for pattern in get_currency_patterns(currency):
        matches = re.findall(
            pattern,
            page_text,
            flags=re.IGNORECASE,
        )

        for match in matches:
            amount = normalise_amount(match)

            if amount is not None:
                detected_prices.append(amount)

    return sorted(set(detected_prices))


def dismiss_cookie_banner(page) -> None:
    button_names = [
        "Accept all",
        "Accept All",
        "Accept cookies",
        "Allow all",
        "I accept",
        "Agree",
        "Continue without accepting",
    ]

    for name in button_names:
        try:
            button = page.get_by_role(
                "button",
                name=re.compile(
                    rf"^{re.escape(name)}$",
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


def load_previous_price() -> float | None:
    if not PREVIOUS_PRICE_PATH.exists():
        return None

    try:
        saved_data = json.loads(
            PREVIOUS_PRICE_PATH.read_text(
                encoding="utf-8"
            )
        )

        saved_price = saved_data.get("lowest_price")

        if saved_price is None:
            return None

        return float(saved_price)

    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return None


def save_previous_price(price: float) -> None:
    data = {
        "hotel": HOTEL_NAME,
        "lowest_price": price,
        "currency": CURRENCY,
        "saved_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    PREVIOUS_PRICE_PATH.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def send_email(
    subject: str,
    body: str,
    attachment: Path | None = None,
) -> None:
    if not EMAIL_FROM:
        raise RuntimeError(
            "EMAIL_FROM has not been configured."
        )

    if not EMAIL_PASSWORD:
        raise RuntimeError(
            "EMAIL_PASSWORD has not been configured."
        )

    if not EMAIL_TO:
        raise RuntimeError(
            "EMAIL_TO has not been configured."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = EMAIL_FROM
    message["To"] = EMAIL_TO
    message.set_content(body)

    if attachment and attachment.exists():
        image_data = attachment.read_bytes()

        message.add_attachment(
            image_data,
            maintype="image",
            subtype="png",
            filename=attachment.name,
        )

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT,
        timeout=45,
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


def should_send_email(
    lowest_price: float | None,
    previous_price: float | None,
    successful: bool,
) -> bool:
    if not successful:
        # Always email when a check fails.
        return True

    if lowest_price is None:
        return True

    if EMAIL_MODE == "target":
        return (
            TARGET_PRICE > 0
            and lowest_price <= TARGET_PRICE
        )

    if EMAIL_MODE == "change":
        return (
            previous_price is None
            or lowest_price != previous_price
        )

    # Default: email after every run.
    return True


def write_github_summary(
    status: str,
    lowest_price: float | None,
    detected_prices: list[float],
    previous_price: float | None,
) -> None:
    summary_file = os.environ.get(
        "GITHUB_STEP_SUMMARY"
    )

    if not summary_file:
        return

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

    if lowest_price is not None:
        lines.extend(
            [
                (
                    "**Lowest detected price:** "
                    f"{CURRENCY} {lowest_price:,.2f}"
                ),
                "",
            ]
        )

    if previous_price is not None:
        lines.extend(
            [
                (
                    "**Previously recorded price:** "
                    f"{CURRENCY} {previous_price:,.2f}"
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
            (
                "**Number of possible prices detected:** "
                f"{len(detected_prices)}"
            ),
            "",
            f"[Open the Accor booking page]({ACCOR_URL})",
            "",
        ]
    )

    with open(
        summary_file,
        "a",
        encoding="utf-8",
    ) as summary:
        summary.write("\n".join(lines))


def check_accor_price() -> tuple[list[float], str]:
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
            print(f"Opening: {ACCOR_URL}")

            response = page.goto(
                ACCOR_URL,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            if response is not None:
                print(
                    "HTTP response status: "
                    f"{response.status}"
                )

            dismiss_cookie_banner(page)

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=30_000,
                )

            except PlaywrightTimeoutError:
                print(
                    "The page did not become completely "
                    "idle. Continuing."
                )

            # Give Accor's scripts time to render prices.
            page.wait_for_timeout(12_000)

            page.screenshot(
                path=str(SCREENSHOT_PATH),
                full_page=True,
            )

            page_text = page.locator(
                "body"
            ).inner_text(timeout=30_000)

            title = page.title()

            print(f"Page title: {title}")

            prices = extract_prices(
                page_text,
                CURRENCY,
            )

            return prices, title

        finally:
            context.close()
            browser.close()


def build_success_email(
    lowest_price: float,
    previous_price: float | None,
    detected_prices: list[float],
) -> tuple[str, str]:
    target_reached = (
        TARGET_PRICE > 0
        and lowest_price <= TARGET_PRICE
    )

    if target_reached:
        subject = (
            "Pullman Khao Lak price alert: "
            f"{CURRENCY} {lowest_price:,.2f}"
        )
    else:
        subject = (
            "Pullman Khao Lak price check: "
            f"{CURRENCY} {lowest_price:,.2f}"
        )

    lines = [
        "Pullman Khao Lak Resort price check",
        "",
        (
            "Lowest detected price: "
            f"{CURRENCY} {lowest_price:,.2f}"
        ),
    ]

    if previous_price is not None:
        difference = lowest_price - previous_price

        if difference < 0:
            lines.append(
                "Price movement: down "
                f"{CURRENCY} {abs(difference):,.2f}"
            )
        elif difference > 0:
            lines.append(
                "Price movement: up "
                f"{CURRENCY} {difference:,.2f}"
            )
        else:
            lines.append("Price movement: no change")

        lines.append(
            "Previous price: "
            f"{CURRENCY} {previous_price:,.2f}"
        )

    if TARGET_PRICE > 0:
        lines.extend(
            [
                (
                    "Target price: "
                    f"{CURRENCY} {TARGET_PRICE:,.2f}"
                ),
                (
                    "Target reached: "
                    f"{'YES' if target_reached else 'NO'}"
                ),
            ]
        )

    lines.extend(
        [
            "",
            (
                "Possible prices detected: "
                + ", ".join(
                    f"{CURRENCY} {price:,.2f}"
                    for price in detected_prices[:10]
                )
            ),
            "",
            "Check the live rate here:",
            ACCOR_URL,
            "",
            (
                "The attached screenshot shows the Accor "
                "page seen during this check."
            ),
            "",
            (
                "Hotel prices can vary by room type, "
                "membership, taxes, meals and cancellation "
                "conditions. Confirm the final booking total "
                "on Accor before paying."
            ),
        ]
    )

    return subject, "\n".join(lines)


def main() -> int:
    print(f"Hotel: {HOTEL_NAME}")
    print(f"Recipient: {EMAIL_TO}")
    print(f"Currency: {CURRENCY}")
    print(f"Email mode: {EMAIL_MODE}")

    previous_price = load_previous_price()

    try:
        detected_prices, page_title = (
            check_accor_price()
        )

    except Exception as error:
        error_message = (
            f"{type(error).__name__}: {error}"
        )

        print(
            "The Accor price check failed: "
            f"{error_message}"
        )

        result = {
            "checked_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "hotel": HOTEL_NAME,
            "hotel_code": HOTEL_CODE,
            "successful": False,
            "error": error_message,
            "url": ACCOR_URL,
        }

        RESULT_PATH.write_text(
            json.dumps(result, indent=2),
            encoding="utf-8",
        )

        write_github_summary(
            status="Check failed",
            lowest_price=None,
            detected_prices=[],
            previous_price=previous_price,
        )

        try:
            send_email(
                subject=(
                    "Pullman Khao Lak price check failed"
                ),
                body=(
                    "The automated Accor price check failed.\n\n"
                    f"Error: {error_message}\n\n"
                    f"Accor URL:\n{ACCOR_URL}\n\n"
                    "Open the GitHub Actions run and inspect "
                    "the log and screenshot."
                ),
                attachment=SCREENSHOT_PATH,
            )

        except Exception as email_error:
            print(
                "The failure email could not be sent: "
                f"{email_error}"
            )

        return 1

    if not detected_prices:
        print(
            f"No {CURRENCY} prices were detected."
        )

        result = {
            "checked_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "hotel": HOTEL_NAME,
            "hotel_code": HOTEL_CODE,
            "successful": False,
            "status": "No prices detected",
            "currency": CURRENCY,
            "page_title": page_title,
            "url": ACCOR_URL,
        }

        RESULT_PATH.write_text(
            json.dumps(result, indent=2),
            encoding="utf-8",
        )

        write_github_summary(
            status="No prices detected",
            lowest_price=None,
            detected_prices=[],
            previous_price=previous_price,
        )

        try:
            send_email(
                subject=(
                    "Pullman Khao Lak: "
                    "no price detected"
                ),
                body=(
                    "The Accor page opened, but the tracker "
                    f"could not find a {CURRENCY} price.\n\n"
                    "Possible reasons:\n"
                    "- The booking URL does not contain dates.\n"
                    "- The hotel is unavailable for the dates.\n"
                    "- Accor displayed a different currency.\n"
                    "- Accor changed the page layout.\n"
                    "- Accor blocked the automated browser.\n\n"
                    f"Page title: {page_title}\n\n"
                    f"Accor URL:\n{ACCOR_URL}\n\n"
                    "The page screenshot is attached."
                ),
                attachment=SCREENSHOT_PATH,
            )

        except Exception as email_error:
            print(
                "The email could not be sent: "
                f"{email_error}"
            )

        return 1

    lowest_price = detected_prices[0]

    print(
        "Lowest detected price: "
        f"{CURRENCY} {lowest_price:,.2f}"
    )

    result = {
        "checked_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "hotel": HOTEL_NAME,
        "hotel_code": HOTEL_CODE,
        "successful": True,
        "currency": CURRENCY,
        "target_price": TARGET_PRICE,
        "previous_price": previous_price,
        "lowest_price": lowest_price,
        "prices_detected": detected_prices,
        "page_title": page_title,
        "email_recipient": EMAIL_TO,
        "url": ACCOR_URL,
    }

    RESULT_PATH.write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )

    email_required = should_send_email(
        lowest_price=lowest_price,
        previous_price=previous_price,
        successful=True,
    )

    if email_required:
        subject, body = build_success_email(
            lowest_price=lowest_price,
            previous_price=previous_price,
            detected_prices=detected_prices,
        )

        try:
            send_email(
                subject=subject,
                body=body,
                attachment=SCREENSHOT_PATH,
            )

        except Exception as email_error:
            print(
                "Price was checked, but the email "
                f"failed: {email_error}"
            )

            write_github_summary(
                status="Price found, but email failed",
                lowest_price=lowest_price,
                detected_prices=detected_prices,
                previous_price=previous_price,
            )

            return 1

    else:
        print(
            "The price was checked, but the email "
            "conditions were not met."
        )

    save_previous_price(lowest_price)

    target_reached = (
        TARGET_PRICE > 0
        and lowest_price <= TARGET_PRICE
    )

    status = (
        "Target reached and email sent"
        if target_reached and email_required
        else "Price checked and email sent"
        if email_required
        else "Price checked; no email required"
    )

    write_github_summary(
        status=status,
        lowest_price=lowest_price,
        detected_prices=detected_prices,
        previous_price=previous_price,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

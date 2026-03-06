import logging
import json
import base64
import hashlib
import time
import os
from typing import Optional, Tuple

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ───────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

CHALLENGE_URL = "https://ceir.gov.mm/openapi/API/Auth/altcha/altcha"
VERIFY_URL = "https://ceir.gov.mm/openapi/API/IMEI/Verify"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://ceir.gov.mm",
    "Referer": "https://ceir.gov.mm/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"'
}

MAX_CONCURRENT_CHECKS = 3

session = requests.Session()
session.headers.update(HEADERS)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────
# ALTCHA + CEIR logic
# ───────────────────────────────────────────────

def fetch_challenge() -> dict:
    try:
        r = session.get(CHALLENGE_URL, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Challenge fetch failed: {e}")
        raise


def solve_pow_worker(args: Tuple[str, str, int, int]) -> Optional[int]:
    salt, challenge, start, end = args
    for number in range(start, end + 1):
        input_str = salt + str(number)
        hash_hex = hashlib.sha256(input_str.encode('utf-8')).hexdigest()
        if hash_hex == challenge:
            return number
    return None


def solve_pow(salt: str, challenge: str, maxnumber: int, workers: int = 4) -> Tuple[int, int]:
    start_time = time.time()

    chunk_size = max(1, (maxnumber + 1) // workers)
    ranges = [
        (salt, challenge, i * chunk_size, min((i + 1) * chunk_size - 1, maxnumber))
        for i in range(workers)
    ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(solve_pow_worker, ranges))

    number = next((res for res in results if res is not None), None)
    if number is None:
        raise ValueError("No PoW solution found — challenge may be expired or invalid")

    took_ms = int((time.time() - start_time) * 1000)
    return number, took_ms


def build_altcha_token(challenge_data: dict, number: int, took: int) -> str:
    payload = {
        "algorithm": challenge_data["algorithm"],
        "challenge": challenge_data["challenge"],
        "number": number,
        "salt": challenge_data["salt"],
        "signature": challenge_data["signature"],
        "took": took
    }
    payload_json = json.dumps(payload, separators=(',', ':'))
    return base64.b64encode(payload_json.encode()).decode()


def check_single_imei(imei: str) -> str:
    imei = imei.strip()
    if not (14 <= len(imei) <= 15 and imei.isdigit()):
        return f"⚠️ Invalid IMEI: {imei} (must be 14–15 digits)"

    try:
        challenge_data = fetch_challenge()
        number, took = solve_pow(
            salt=challenge_data["salt"],
            challenge=challenge_data["challenge"],
            maxnumber=challenge_data["maxnumber"]
        )
        altcha = build_altcha_token(challenge_data, number, took)

        full_url = f"{VERIFY_URL}?altcha={altcha}"
        payload = [imei]

        r = session.post(full_url, data=json.dumps(payload), timeout=15)
        r.raise_for_status()

        data = r.json()
        if "IMEI_CHECK_LIST" not in data or not data["IMEI_CHECK_LIST"]:
            return f"❌ {imei} → No data returned"

        item = data["IMEI_CHECK_LIST"][0]
        dev = item.get("deviceInfo", {})

        # ───────────────────────────────────────────────
        # Vital fields mapping
        # ───────────────────────────────────────────────
        brand_model = f"{dev.get('gsmaBrandName', '—')} {dev.get('gsmaModelName', '—')}"
        payment_state = item.get("paymentState", "UNKNOWN")
        block_status = item.get("blockState", "UNKNOWN")

        # IMEI Status: Correct / Incorrect
        wrong_format = item.get("WrongFormat", False)
        incorrect = item.get("Incorrect", False)
        imei_status = "Correct" if not wrong_format and not incorrect else "Incorrect"

        white_listed = "Yes" if item.get("WhiteList") else "No"
        black_listed = "Yes" if item.get("BlackList") else "No"

        # Extra device info you requested
        gsma_model_name = dev.get("gsmaModelName", "—")
        gsma_marketing_name = dev.get("gsmaMarketingName", "—")
        gsma_allocation_date = dev.get("gsmaAllocationDate", "—")
        gsma_os = dev.get("gsmaOperatingSystem", "—")

        # ───────────────────────────────────────────────
        # Short vital summary (first part)
        # ───────────────────────────────────────────────
        vital_summary = [
            f"**IMEI:** {imei}",
            f"**Device:** {brand_model}",
            f"**IMEI Status:** {imei_status}",
            f"**Tax Payment Status:** {payment_state}",
            f"**Device blocking status:** {block_status}"
        ]

        # ───────────────────────────────────────────────
        # Full detailed result
        # ───────────────────────────────────────────────
        full_details = [
            f"📱 **Full Check Result for {imei}**",
            f"• Brand / Model: {brand_model}",
            f"• IMEI Status: {imei_status} {'(valid format & recognized)' if imei_status == 'Correct' else '(invalid format or unrecognized)'}",
            f"• Tax Payment Status: {payment_state}",
            f"• Device blocking status: {block_status}",
            f"• Whitelisted: {white_listed}",
            f"• Blacklisted: {black_listed}",
            "",  # empty line before extra info
            "**Extra Device Information:**",
            f"• Internal Model: {gsma_model_name}",
            f"• Marketing Name: {gsma_marketing_name}",
            f"• Allocation Date: {gsma_allocation_date}",
            f"• Operating System: {gsma_os}",
        ]

        # Combine
        return "\n".join(vital_summary) + "\n\n" + "\n".join(full_details)

    except Exception as e:
        logger.error(f"IMEI check failed for {imei}: {e}")
        return f"❌ {imei} → Error: {str(e)}"


# ───────────────────────────────────────────────
# Telegram handlers
# ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "CEIR Myanmar IMEI Checker Bot\n\n"
        "Usage:\n"
        "/check 865163040845331\n"
        "/check 865163040845331 355678901234567\n\n"
        "Supports up to 10 IMEIs at once."
    )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Please provide at least one IMEI.\nExample: /check 865163040845331")
        return

    imei_list = context.args[:10]
    status_msg = await update.message.reply_text(
        f"🔍 Checking {len(imei_list)} IMEI(s) …"
    )

    results = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHECKS) as executor:
        future_to_imei = {
            executor.submit(check_single_imei, imei): imei
            for imei in imei_list
        }

        for future in as_completed(future_to_imei):
            results.append(future.result())

    # Preserve order
    ordered = []
    for imei in imei_list:
        for res in results:
            if imei in res:
                ordered.append(res)
                break

    text = "\n\n".join(ordered)
    await status_msg.edit_text(text)


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set. Exiting.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", check))

    logger.info("Starting bot (polling mode)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

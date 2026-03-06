import logging
import json
import base64
import hashlib
import time
from typing import Optional, Tuple, List

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ───────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────
BOT_TOKEN = "8311754107:AAHvOIMf4JQ-iuLfRci3cP2bEOD2g8jpiWU"

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

# How many parallel IMEI checks at once (start low!)
MAX_WORKERS = 3

# Global session for connection reuse
session = requests.Session()
session.headers.update(HEADERS)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────
# ALTCHA + CEIR logic (same as your script, slightly adapted)
# ───────────────────────────────────────────────

def fetch_challenge() -> dict:
    try:
        r = session.get(CHALLENGE_URL, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Challenge fetch failed: {e}")


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

    chunk_size = (maxnumber + 1) // workers
    ranges = [
        (salt, challenge, i * chunk_size, min((i + 1) * chunk_size - 1, maxnumber))
        for i in range(workers)
    ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(solve_pow_worker, ranges)

    number = next((res for res in results if res is not None), None)
    if number is None:
        raise ValueError("No solution found — expired/invalid challenge?")

    took_ms = int((time.time() - start_time) * 1000)
    return number, took_ms


def build_altcha_token(challenge_data: dict, number: int, took: int) -> str:
    payload_dict = {
        "algorithm": challenge_data["algorithm"],
        "challenge": challenge_data["challenge"],
        "number": number,
        "salt": challenge_data["salt"],
        "signature": challenge_data["signature"],
        "took": took
    }
    payload_json = json.dumps(payload_dict, separators=(',', ':'))
    return base64.b64encode(payload_json.encode('utf-8')).decode('utf-8')


def verify_one_imei(imei: str) -> str:
    """Returns formatted string — success or error message"""
    imei = imei.strip()
    if not imei.isdigit() or len(imei) not in (14, 15):
        return f"⚠️ Invalid IMEI format: {imei}"

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
            return f"❌ {imei} → No result returned"

        item = data["IMEI_CHECK_LIST"][0]

        brand = item.get("deviceInfo", {}).get("gsmaBrandName", "Unknown")
        model = item.get("deviceInfo", {}).get("gsmaModelName", "Unknown")
        status = item.get("blockState", "UNKNOWN")
        white = "Yes" if item.get("WhiteList") else "No"
        black = "Yes" if item.get("BlackList") else "No"

        lines = [
            f"📱 {imei}",
            f"• Brand/Model: {brand} {model}",
            f"• Block status: {status}",
            f"• White-listed: {white}",
            f"• Black-listed: {black}",
        ]

        # Optional: add more fields if you want
        return "\n".join(lines)

    except Exception as e:
        return f"❌ {imei} → Error: {str(e)}"


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/check 865163040845331\nor\n/check 865163040845331 355678901234567"
        )
        return

    imei_list = context.args[:10]  # safety limit
    msg = await update.message.reply_text(f"🔍 Checking {len(imei_list)} IMEI(s)...")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_imei = {executor.submit(verify_one_imei, imei): imei for imei in imei_list}

        for future in as_completed(future_to_imei):
            result_text = future.result()
            results.append(result_text)

    # Sort back to input order (optional)
    ordered_results = []
    for imei in imei_list:
        for res in results:
            if imei in res:
                ordered_results.append(res)
                break

    final_text = "CEIR Check Results:\n\n" + "\n\n".join(ordered_results)
    await msg.edit_text(final_text, disable_web_page_preview=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "CEIR IMEI Checker Bot\n\n"
        "Send /check <imei> [<imei2> ...]\n"
        "Example: /check 865163040845331"
    )


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", cmd_check))

    # Optional: catch unknown commands or messages
    # application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    print("Bot started. Polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

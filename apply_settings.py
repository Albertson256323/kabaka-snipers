# ==========================================
# FILE: apply_settings.py
#
# Parses a submitted "Bot Settings" issue (see .github/ISSUE_TEMPLATE/settings.yml)
# and writes config.json accordingly. Run by .github/workflows/apply-settings.yml
# whenever a settings issue is opened.
# ==========================================
import json
import os
import re
import sys

ALL_PAIRS = ["ETH/USDT", "SOL/USDT"]


def extract_section(body, label):
    """Pulls the text under '### <label>' up to the next '###' or end of body."""
    pattern = rf"###\s*{re.escape(label)}\s*\n+(.*?)(?=\n###|\Z)"
    match = re.search(pattern, body, re.DOTALL)
    return match.group(1).strip() if match else ""


def parse_settings(body):
    balance_raw = extract_section(body, "Starting Balance (USD)")
    try:
        balance = float(re.sub(r"[^\d.]", "", balance_raw))
    except (ValueError, TypeError):
        balance = None

    mode_raw = extract_section(body, "Trade Mode").lower()
    trade_mode = "multi" if mode_raw.startswith("multi") else "single" if mode_raw.startswith("single") else None

    pairs_raw = extract_section(body, "Pairs to Scan")
    pairs = [p for p in ALL_PAIRS if re.search(rf"\[[xX]\]\s*{re.escape(p)}", pairs_raw)]

    run_raw = extract_section(body, "Run Duration").strip()
    if run_raw.lower().startswith("no limit"):
        run_days = None
    else:
        try:
            run_days = int(re.sub(r"[^\d]", "", run_raw))
        except (ValueError, TypeError):
            run_days = None

    return {
        "starting_balance": balance,
        "trade_mode": trade_mode,
        "pairs": pairs if pairs else None,
        "run_days": run_days,
        "_run_raw": run_raw
    }


def main():
    body = os.environ.get("ISSUE_BODY", "")
    parsed = parse_settings(body)

    errors = []
    if parsed["starting_balance"] is None or parsed["starting_balance"] <= 0:
        errors.append("Could not read a valid Starting Balance.")
    if parsed["trade_mode"] is None:
        errors.append("Could not read a valid Trade Mode.")
    if not parsed["pairs"]:
        errors.append("At least one pair must be checked under Pairs to Scan.")

    if errors:
        print("SETTINGS_RESULT=failed")
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("result=failed\n")
            f.write("message=" + " ".join(errors).replace("\n", " ") + "\n")
        sys.exit(0)

    config = {
        "starting_balance": parsed["starting_balance"],
        "trade_mode": parsed["trade_mode"],
        "pairs": parsed["pairs"],
        "run_days": parsed["run_days"]
    }
    with open("config.json", "w") as f:
        json.dump(config, f, indent=2)

    summary = (
        f"Balance: ${parsed['starting_balance']:.2f} | "
        f"Mode: {parsed['trade_mode']} | "
        f"Pairs: {', '.join(parsed['pairs'])} | "
        f"Run: {parsed['_run_raw'] if parsed['run_days'] else 'No limit'}"
    )
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write("result=success\n")
        f.write("message=" + summary + "\n")


if __name__ == "__main__":
    main()

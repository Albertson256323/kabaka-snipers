# ==========================================
# FILE: apply_settings.py
#
# Parses a submitted Settings issue (Swing or Intraday form) and merges the
# result into config.json's per-strategy structure WITHOUT touching the
# other strategy's settings. Which strategy is determined by the issue
# title ("[Settings: Swing]" or "[Settings: Intraday]").
# Run by .github/workflows/apply-settings.yml whenever a settings issue opens.
# ==========================================
import json
import os
import re
import sys

ALL_PAIRS = ["ETH/USDT", "SOL/USDT"]
CONFIG_PATH = "config.json"


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

    launch_raw = extract_section(body, "Launch Switch")
    enabled = bool(re.search(r"\[[xX]\]\s*Arm", launch_raw))

    return {
        "starting_balance": balance,
        "trade_mode": trade_mode,
        "pairs": pairs if pairs else None,
        "run_days": run_days,
        "enabled": enabled,
        "_run_raw": run_raw
    }


def main():
    title = os.environ.get("ISSUE_TITLE", "")
    body = os.environ.get("ISSUE_BODY", "")

    if "intraday" in title.lower():
        strategy = "intraday"
    elif "swing" in title.lower():
        strategy = "swing"
    else:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("result=failed\n")
            f.write("message=Couldn't tell which strategy this settings issue is for from the title.\n")
        sys.exit(0)

    parsed = parse_settings(body)

    errors = []
    if parsed["starting_balance"] is None or parsed["starting_balance"] <= 0:
        errors.append("Could not read a valid Starting Balance.")
    if parsed["trade_mode"] is None:
        errors.append("Could not read a valid Trade Mode.")
    if not parsed["pairs"]:
        errors.append("At least one pair must be checked under Pairs to Scan.")

    if errors:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("result=failed\n")
            f.write("message=" + " ".join(errors).replace("\n", " ") + "\n")
        sys.exit(0)

    # Load existing config so the OTHER strategy's settings are untouched
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            full_config = json.load(f)
    else:
        full_config = {"strategies": {}}
    if "strategies" not in full_config:
        full_config = {"strategies": full_config}

    full_config["strategies"][strategy] = {
        "enabled": parsed["enabled"],
        "starting_balance": parsed["starting_balance"],
        "trade_mode": parsed["trade_mode"],
        "pairs": parsed["pairs"],
        "lock_days": parsed["run_days"]
    }

    with open(CONFIG_PATH, "w") as f:
        json.dump(full_config, f, indent=2)

    summary = (
        f"Strategy: {strategy} | Launch: {'ARMED' if parsed['enabled'] else 'disarmed'} | "
        f"Balance: ${parsed['starting_balance']:.2f} | Mode: {parsed['trade_mode']} | "
        f"Pairs: {', '.join(parsed['pairs'])} | Lock: {parsed['_run_raw'] if parsed['run_days'] else 'No limit'}"
    )
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write("result=success\n")
        f.write("message=" + summary + "\n")


if __name__ == "__main__":
    main()

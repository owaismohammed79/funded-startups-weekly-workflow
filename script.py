import os
import time
import json
import re
import smtplib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import gspread
from groq import Groq
from tavily import TavilyClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")   

REQUIRED_ENV_VARS = {
    "TAVILY_API_KEY": TAVILY_API_KEY,
    "SENDER_EMAIL": SENDER_EMAIL,
    "SPREADSHEET_ID": SPREADSHEET_ID,
}
missing = [name for name, val in REQUIRED_ENV_VARS.items() if not val]
if missing:
    raise RuntimeError(
        f"Missing required environment variable(s): {', '.join(missing)}. "
        "Set these as GitHub Actions secrets before running."
    )
if not GROQ_API_KEY:
    raise RuntimeError(
        "GROQ_API_KEY must be set — there is no LLM provider configured otherwise."
    )

groq_client = Groq(api_key=GROQ_API_KEY)

FUNDS_TO_TRACK = [
    "South Park Commons",
    "Founders Fund",
    "Y Combinator",
    "Sequoia Capital",
    "Andreessen Horowitz",
    "Lightspeed Venture Partners",
    "First Round Capital",
]

FUNDING_WINDOW_DAYS = 270
EXHAUSTED_MODELS = set()
MAX_NEW_STARTUPS_PER_RUN = 20

STATE_DIR = "data/state"
LEDGER_PATH = os.path.join(STATE_DIR, "seen_startups.json")
REPORT_PATH = "sourcing_report.json"

MODEL_CASCADE = [
    ("groq", "llama-3.3-70b-versatile"),
    ("groq", "openai/gpt-oss-120b"),
    ("groq", "meta-llama/llama-4-scout-17b-16e-instruct"),
]

MAX_WAIT_THRESHOLD = 30.0


def clean_and_parse_json(raw_text: str):
    cleaned = raw_text.strip()
    cleaned = re.sub(r'<think>.*?</think>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'^```json\s*', '', cleaned.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'^```\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)
    return json.loads(cleaned.strip())

def save_state_to_json(data: list, filename: str = REPORT_PATH):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    print(f"    State synchronized cleanly to {filename}")


def dynamic_start_date(days_back: int = FUNDING_WINDOW_DAYS) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    return cutoff.strftime("%Y-%m-%d")


def load_ledger() -> dict:
    if not os.path.exists(LEDGER_PATH):
        return {"seen": {}, "last_run": None}
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[!] Could not read ledger at {LEDGER_PATH} ({e}); starting fresh.")
        return {"seen": {}, "last_run": None}


def save_ledger(ledger: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    ledger["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(LEDGER_PATH, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)
    print(f"     Ledger updated at {LEDGER_PATH} "
          f"({len(ledger['seen'])} startups tracked total).")


def parse_wait_time(error_str: str) -> float:
    time_sec = 0.0
    m_match = re.search(r'(\d+)m', error_str)
    s_match = re.search(r'(\d+(?:\.\d+)?)s', error_str)
    if m_match:
        time_sec += int(m_match.group(1)) * 60
    if s_match:
        time_sec += float(s_match.group(1))
    return time_sec if time_sec > 0 else 15.0


def _call_groq(model: str, prompt: str) -> str:
    response = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": "You are a precise data extraction assistant. You must output valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        model=model,
        response_format={"type": "json_object"},
        temperature=0,
    )
    return response.choices[0].message.content


def generate_with_fallback(prompt: str) -> str:
    global EXHAUSTED_MODELS
    
    for provider, model in MODEL_CASCADE:
        if model in EXHAUSTED_MODELS:
            continue
            
        attempt = 0
        while attempt < 3:
            try:
                text = _call_groq(model, prompt)
                return text
            except Exception as e:
                error_str = str(e).lower()

                if "request too large" in error_str or ("tpm" in error_str and "per day" not in error_str):
                    print(f"    [!] Payload too large for {provider}/{model}. Cascading...")
                    break

                if "tpd" in error_str or "per day" in error_str or ("resource_exhausted" in error_str and "day" in error_str):
                    print(f"    [!] Daily limits exhausted on {provider}/{model}. Blacklisting model globally...")
                    EXHAUSTED_MODELS.add(model)
                    break

                if "429" in error_str or "rate" in error_str or "resource_exhausted" in error_str:
                    wait_time = parse_wait_time(error_str)
                    if wait_time > MAX_WAIT_THRESHOLD:
                        print(f"    [!] Rate limited on {provider}/{model}. Wait is {wait_time}s. Cascading immediately...")
                        break
                    else:
                        print(f"    Rate limited on {provider}/{model}. Waiting {wait_time}s...")
                        time.sleep(wait_time + 1)
                        attempt += 1
                        continue

                print(f"    [!] {provider}/{model} failed: {e}. Cascading...")
                break
    print("     All models in cascade exhausted or failed for this prompt.")
    return "{}"

def with_retry(max_retries=3, delay=5):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        print(f"    [!] {func.__name__} failed after {max_retries} attempts: {e}")
                        return None
                    time.sleep(delay)
        return wrapper
    return decorator

@with_retry(max_retries=3, delay=3)
def search_tavily_general(query: str, start_date: str = None, search_depth: str = "advanced", max_results: int = 3) -> str:
    params = {"query": query, "search_depth": search_depth, "max_results": max_results}
    if start_date:
        params["start_date"] = start_date
    response = tavily_client.search(**params)
    results = response.get("results", [])
    return "\n---\n".join(
        f"Title: {r.get('title', 'Unknown')}\nURL: {r.get('url', '')}\nContent: {r.get('content', '')}"
        for r in results
    )


@with_retry(max_retries=3, delay=3)
def search_tavily_social(query: str, include_domains=None, search_depth: str = "advanced", max_results: int = 5) -> str:
    params = {
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
    }
    if include_domains:
        params["include_domains"] = include_domains
    response = tavily_client.search(**params)
    results = response.get("results", [])
    return "\n---\n".join(f"URL: {r.get('url', 'Unknown')}\nContent: {r.get('content', '')}" for r in results)


tavily_client = TavilyClient(api_key=TAVILY_API_KEY)


def extract_startups(source_name: str, raw_text: str) -> list:
    if not raw_text or not raw_text.strip():
        return []

    prompt = f"""
    Analyze this raw tech ecosystem material regarding '{source_name}'.

    CRITICAL FILTERING RULES:
    1. ONLY extract startups where the text indicates the Pre-Seed, Seed, or Series A round is the CURRENT or most recent state of the business within the given timeline.
    2. TIMELINE ANCHOR: The round must be a current announcement. EXCLUDE historical context bios tracking old achievements of now-famous companies.
    3. CORPORATE MATURITY KILL-SWITCH: STRICTLY EXCLUDE well-known tech giants, public corporations, unicorns, or market leaders (e.g., Elastic, Elasticsearch, Clay, Harvey, Sierra, Temporal), even if the text mentions their historical early-stage rounds.
    4. PROOF REQUIREMENT: You MUST provide a verbatim quote from the text that proves the early-stage funding event.

    Return strictly a JSON object formatted exactly like this:
    {{
        "startups": [
            {{
                "name": "StartupName",
                "evidence_quote": "The exact sentence from the text proving the early-stage round."
            }}
        ]
    }}
    If no matches survive these constraints, return {{"startups": []}}.
    Data:
    {raw_text}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        data = clean_and_parse_json(response_payload)
        valid_startups = []
        normalized_raw = " ".join(raw_text.split())

        for item in data.get("startups", []):
            name = item.get("name")
            quote = item.get("evidence_quote", "")
            normalized_quote = " ".join(quote.split())

            if name and normalized_quote and (
                normalized_quote in normalized_raw or normalized_quote[:30] in normalized_raw
            ):
                valid_startups.append(name)
            else:
                print(f"        [-] Hallucination intercepted: Destroying unverified data for '{name}'")

        return valid_startups
    except Exception:
        return []

def extract_founder_names(startup: str, fund: str) -> list:
    primary_query = f'"{startup}" company founder'
    raw_intel = search_tavily_general(primary_query, search_depth="advanced", max_results=6)

    if not raw_intel or not raw_intel.strip():
        fallback_query = f'"{startup}" startup funded "{fund}" founder co-founder'
        print(f"        [~] Primary founder search empty for '{startup}'. Retrying with fund context...")
        raw_intel = search_tavily_general(fallback_query, search_depth="advanced", max_results=6)

    if not raw_intel:
        return []

    prompt = f"""
    Extract the names of the founders for the startup '{startup}'.
    Return strictly a JSON object with a single key "founders" containing an array of strings (their names).
    If no names are found, return {{"founders": []}}.
    Data: {raw_intel}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        return clean_and_parse_json(response_payload).get("founders", [])
    except Exception:
        return []

def enrich_specific_founder(startup: str, founder_name: str) -> dict:
    query = f'"{founder_name}" "{startup}" LinkedIn Twitter X profile'
    raw_intel = search_tavily_social(
        query,
        include_domains=["linkedin.com", "twitter.com", "x.com"],
        search_depth="advanced",
        max_results=5,
    )

    if not raw_intel:
        return {"founder_name": founder_name, "linkedin": [], "x_handle": []}

    prompt = f"""
    Parse this data to find the social profiles for '{founder_name}', founder of '{startup}'.
    Return strictly a JSON object with exactly these keys:
    "founder_name": "{founder_name}",
    "linkedin": [Array of strings containing their actual LinkedIn URLs from the data],
    "x_handle": [Array of strings containing their actual X/Twitter profile URLs]

    Only include a URL if the surrounding text in the data ties it to '{founder_name}' specifically —
    do not include profiles belonging to other people who share a similar name.
    If valid profile URLs are missing, return empty arrays [].
    Material:
    {raw_intel}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        data = clean_and_parse_json(response_payload)
        if isinstance(data.get("linkedin"), str):
            data["linkedin"] = [data["linkedin"]]
        if isinstance(data.get("x_handle"), str):
            data["x_handle"] = [data["x_handle"]]
        return data
    except Exception:
        return {"founder_name": founder_name, "linkedin": [], "x_handle": []}

def format_links(links_array: list, label: str) -> str:
    if not links_array or not isinstance(links_array, list):
        return "N/A"
    clean_links = []
    toxic_patterns = ["/search", "?q=", "/hashtag/", "/status/", "/posts/"]

    for link in links_array:
        if link and isinstance(link, str):
            link = link.strip()
            if any(toxic in link.lower() for toxic in toxic_patterns):
                continue
            if link not in ["", "#", "N/A", "Not Found"] and link.startswith("http"):
                clean_links.append(link)

    if not clean_links:
        return "N/A"
    return "<br>".join(f"<a href='{url}' target='_blank'>{label} {i+1}</a>" for i, url in enumerate(clean_links))


def fetch_form_subscribers() -> list:
    """Connects to Google Sheets via gspread and extracts unique subscriber emails and names."""
    try:
        # Check for service account json file
        creds_file = "credentials.json"
        if not os.path.exists(creds_file):
            print(f"[-] Service account credentials file '{creds_file}' not found.")
            return []

        gc = gspread.service_account(filename=creds_file)
        sh = gc.open_by_key(SPREADSHEET_ID)
        worksheet = sh.get_worksheet(0)
        records = worksheet.get_all_records()

        subscribers = []
        for row in records:
            email = None
            name = "Subscriber"

            for key, val in row.items():
                clean_key = str(key).strip().lower()
                clean_val = str(val).strip()
                if "email" in clean_key and clean_val:
                    email = clean_val
                elif "name" in clean_key and clean_val:
                    name = clean_val

            if email and re.match(r"[^@]+@[^@]+\.[^@]+", email):
                subscribers.append({"email": email, "name": name})

        unique_map = {item["email"].lower(): item for item in subscribers}
        unique_subscribers = list(unique_map.values())

        print(f"[+] Successfully loaded {len(unique_subscribers)} subscriber(s) from Google Sheet.")
        return unique_subscribers

    except Exception as e:
        print(f"[-] Failed to fetch subscribers from Google Sheets: {e}")
        return []


def send_report(final_data: list):
    if not final_data:
        print("[*] Sourcing engine returned zero NEW early-stage entities for this window.")
        return

    subscribers = fetch_form_subscribers()

    if not subscribers:
        if RECEIVER_EMAIL:
            print("[!] No subscribers found in Sheet. Falling back to default RECEIVER_EMAIL.")
            subscribers = [{"email": RECEIVER_EMAIL, "name": "Subscriber"}]
        else:
            print("[-] CRITICAL EMAIL FAILURE: No recipient subscribers found.")
            return

    table_html = "<table border='1' cellpadding='10' style='border-collapse: collapse;'>"
    table_html += "<tr><th>Source Context</th><th>Startup</th><th>Founders</th><th>LinkedIn Profiles</th><th>X Handles</th></tr>"

    for entry in final_data:
        names_list = entry.get("founder_names", [])
        founder_names_str = ", ".join(names_list) if names_list else "N/A"
        linkedin_html = format_links(entry.get("linkedin", []), "LinkedIn")
        x_html = format_links(entry.get("x_handle", []), "X Profile")

        table_html += f"<tr><td>{entry['fund']}</td><td>{entry['startup']}</td><td>{founder_names_str}</td>"
        table_html += f"<td>{linkedin_html}</td><td>{x_html}</td></tr>"
    table_html += "</table>"

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }

    for sub in subscribers:
        recipient_email = sub["email"]
        recipient_name = sub["name"]

        full_html = f"<h2>Hi {recipient_name},</h2>"
        full_html += "<p>Here is your Weekly Sourcing Report (Verified Early Stage Founders):</p>"
        full_html += table_html

        payload = {
            "sender": {"name": "Sourcing Report", "email": SENDER_EMAIL},
            "to": [{"email": recipient_email, "name": recipient_name}],
            "subject": "High-Signal Sourcing Pipeline: Early Stage Founders",
            "htmlContent": full_html
        }

        try:
            response = requests.post(url, json=payload, headers=headers)
            if response.status_code in (200, 201):
                print(f"    [+] Brevo dispatched report to {recipient_name} ({recipient_email})")
            else:
                print(f"    [-] Brevo API error for {recipient_email}: {response.text}")
        except Exception as e:
            print(f"    [-] Failed to send to {recipient_email} via Brevo: {e}")

def main():
    print(f"[*] Initializing Grounded Sourcing Pipeline. "
          f"Cascade order: {[f'{p}/{m}' for p, m in MODEL_CASCADE]}")

    ledger = load_ledger()
    seen = ledger["seen"]
    start_date = dynamic_start_date()
    print(f"[*] Funding window start date (dynamic, {FUNDING_WINDOW_DAYS}d back): {start_date}")

    compiled_intelligence = []
    newly_seen_this_run = set()

    for fund in FUNDS_TO_TRACK:
        if len(compiled_intelligence) >= MAX_NEW_STARTUPS_PER_RUN:
            print(f"[*] Reached MAX_NEW_STARTUPS_PER_RUN ({MAX_NEW_STARTUPS_PER_RUN}). Stopping fund scan.")
            break

        search_query = f"{fund} early stage funding announced pre-seed seed series A 2026"
        print(f"\n-> Fetching high-signal portfolio indicators for {fund}...")

        raw_news = search_tavily_general(search_query, start_date=start_date)
        if not raw_news:
            continue

        startups = extract_startups(fund, raw_news)
        print(f"    Verified Early Stage Startups: {startups}")

        for index, startup in enumerate(startups):
            if len(compiled_intelligence) >= MAX_NEW_STARTUPS_PER_RUN or index >= 3:
                break

            key = startup.lower()
            if key in seen:
                print(f"    [!] Skipping {startup} - already reported on {seen[key]}.")
                continue
            if key in newly_seen_this_run:
                continue

            newly_seen_this_run.add(key)
            print(f"    [+] Locating founder entities for {startup}...")
            founder_names = extract_founder_names(startup, fund)

            if not founder_names:
                print(f"        [-] No explicit founder names extracted for {startup}.")
                compiled_intelligence.append({
                    "fund": fund,
                    "startup": startup,
                    "founder_names": ["N/A"],
                    "linkedin": [],
                    "x_handle": [],
                })
                continue

            startup_record = {
                "fund": fund,
                "startup": startup,
                "founder_names": [],
                "linkedin": [],
                "x_handle": [],
            }

            for name in founder_names:
                print(f"        [+] Extracting targeted URLs for {name}...")
                founder_profile = enrich_specific_founder(startup, name)
                startup_record["founder_names"].append(founder_profile.get("founder_name", name))
                startup_record["linkedin"].extend(founder_profile.get("linkedin", []))
                startup_record["x_handle"].extend(founder_profile.get("x_handle", []))
                time.sleep(4)

            compiled_intelligence.append(startup_record)
            save_state_to_json(compiled_intelligence)
            time.sleep(3)

    today_iso = datetime.now(timezone.utc).date().isoformat()
    for entry in compiled_intelligence:
        seen[entry["startup"].lower()] = today_iso
    save_ledger(ledger)

    send_report(compiled_intelligence)
    print(f"\n[+] Target pipeline run finalized. {len(compiled_intelligence)} new startup(s) reported.")


if __name__ == "__main__":
    main()
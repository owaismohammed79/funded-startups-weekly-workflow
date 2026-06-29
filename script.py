import os
import time
import json
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from google.genai import errors
from tavily import TavilyClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# -- API Key Configuration --
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

FUNDS_TO_TRACK = [
    "South Park Commons",
    "Founders Fund",
    "Y Combinator",
    "Sequoia Capital",
    "Andreessen Horowitz",
    "Lightspeed Venture Partners",
    "First Round Capital"
]

def clean_and_parse_json(raw_text: str):
    """Strips markdown backticks safely to prevent JSON decoder crashes."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r'^```json\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'^```\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)
    return json.loads(cleaned.strip())

def save_state_to_json(data: list, filename: str = "sourcing_report.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    print(f"    [💾] State synchronized cleanly to {filename}")

def generate_with_fallback(prompt: str) -> str:
    model_pool = [
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite"
    ]
    for model in model_pool:
        try:
            response = ai_client.models.generate_content(model=model, contents=prompt)
            if response.text: return response.text
        except errors.APIError as e:
            if e.code == 429: continue
        except Exception as e:
            if "429" in str(e) or "quota" in str(e).lower(): continue
    return ""

def with_retry(max_retries=3, delay=5):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        print(f"[-] Permanent failure in '{func.__name__}': {e}")
                        return None
                    time.sleep(delay)
        return wrapper
    return decorator

@with_retry(max_retries=3, delay=5)
def search_tavily(query: str) -> str:
    """Uses general deep web indexing to catch portfolio updates, not just news."""
    response = tavily_client.search(query=query, search_depth=1, topic="general")
    results = response.get("results", [])
    return "\n".join([f"Title: {r.get('title')}\nContent: {r.get('content')}" for r in results])

def extract_startups(source_name: str, raw_text: str) -> list:
    if not raw_text or not raw_text.strip(): return []
    prompt = f"""
    Analyze this raw tech ecosystem material regarding '{source_name}'.
    Identify explicit startups or investments associated with them finalized or added recently.
    Return strictly a valid JSON array of strings containing only the clear names of the startups.
    If no matches exist, return an empty array []. Do not wrap the output in markdown fences.
    
    Data:
    {raw_text}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        return clean_and_parse_json(response_payload)
    except Exception as e:
        print(f"    [!] Startup parsing error for {source_name}: {e}")
        return []

def enrich_founder_data(startup: str) -> dict:
    # Highly specific query to find social profiles directly
    query = f"'{startup}' startup founder primary LinkedIn URL Twitter handle"
    raw_intel = search_tavily(query)
    if not raw_intel:
        return {"founder_name": "N/A", "linkedin": "#", "x_handle": "N/A"}
        
    prompt = f"""
    Parse this data to find the founder of the startup '{startup}'.
    Return strictly a clean, valid JSON object containing exactly these keys:
    "founder_name", "linkedin", "x_handle".
    Provide 'Not Found' or '#' if missing from the text. Do not use markdown wrappers.
    
    Material:
    {raw_intel}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        return clean_and_parse_json(response_payload)
    except Exception as e:
        print(f"    [!] Founder data extraction crash for {startup}: {e}")
        return {"founder_name": "N/A", "linkedin": "#", "x_handle": "N/A"}

def send_report(final_data: list):
    if not final_data:
        print("[*] Sourcing engine returned zero entities for this window.")
        return
        
    html = "<h2>Weekly Sourcing Report</h2><table border='1' cellpadding='10' style='border-collapse: collapse;'>"
    html += "<tr><th>Source Context</th><th>Startup</th><th>Founder</th><th>LinkedIn Profile</th><th>X Handle</th></tr>"
    for entry in final_data:
        html += f"<tr><td>{entry['fund']}</td><td>{entry['startup']}</td><td>{entry['founder'].get('founder_name', 'N/A')}</td>"
        html += f"<td><a href='{entry['founder'].get('linkedin', '#')}'>LinkedIn Link</a></td>"
        html += f"<td>{entry['founder'].get('x_handle', 'N/A')}</td></tr>"
    html += "</table>"
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Automated Market Intelligence: Foundational Pipelines"
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg.attach(MIMEText(html, "html"))
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        print("[+] Intelligence digest deployed successfully.")
    except Exception as e:
        print(f"\n[-] CRITICAL EMAIL FAILURE: {e}")

def main():
    compiled_intelligence = []
    print("[*] Initializing Sourcing Pipeline Verification...")
    
    for fund in FUNDS_TO_TRACK:
        print(f"\n-> Fetching current portfolio indicators for {fund}...")
        # Broad query targeting portfolio indexes over strict news articles
        raw_news = search_tavily(f"{fund} recent portfolio companies investments batch")
        
        if raw_news:
            startups = extract_startups(fund, raw_news)
            print(f"    Found Startups: {startups}")
            for startup in startups:
                print(f"    [+] Profiling founder infrastructure: {startup}...")
                founder_profile = enrich_founder_data(startup)
                
                compiled_intelligence.append({"fund": fund, "startup": startup, "founder": founder_profile})
                save_state_to_json(compiled_intelligence)
                time.sleep(2) 
                
    send_report(compiled_intelligence)
    print("\n[+] Core pipeline run finalized.")

if __name__ == "__main__":
    main()
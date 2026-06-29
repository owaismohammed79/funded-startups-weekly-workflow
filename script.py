import os
import time
import json
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
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD") # MUST BE A 16-LETTER GOOGLE APP PASSWORD
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

# Trimmed strictly to Tier-1 Funds to eliminate junk and save credits
FUNDS_TO_TRACK = [
    "South Park Commons",
    "Founders Fund",
    "Y Combinator",
    "Sequoia Capital",
    "Andreessen Horowitz",
    "Lightspeed Venture Partners",
    "First Round Capital"
]

def save_state_to_json(data: list, filename: str = "sourcing_report.json"):
    """Instantly writes data to a local file so you never lose it if the script crashes."""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    print(f"    [💾] Data safely written to {filename}")

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
                    print(f"[!] Transient fault in '{func.__name__}': {e}. Retrying...")
                    time.sleep(delay)
        return wrapper
    return decorator

@with_retry(max_retries=3, delay=5)
def search_tavily(query: str, is_news: bool = False, days: int = 7) -> str:
    if is_news:
        response = tavily_client.search(query=query, search_depth="advanced", topic="news", days=days)
    else:
        response = tavily_client.search(query=query, search_depth="advanced", topic="general")
    results = response.get("results", [])
    return "\n".join([f"Title: {r.get('title')}\nContent: {r.get('content')}" for r in results])

def extract_startups(source_name: str, raw_text: str) -> list:
    if not raw_text or not raw_text.strip(): return []
    prompt = f"""
    Analyze the following raw intelligence data regarding {source_name}.
    Identify any explicit startups that have finalized a recent funding round.
    Return strictly a valid JSON array of strings containing only the clear names of the startups.
    If no matches exist, return an empty array []. Do not wrap the output in markdown.
    Data Chunks: {raw_text}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        return json.loads(response_payload.strip())
    except Exception:
        return []

def enrich_founder_data(startup: str) -> dict:
    # A single, highly targeted search to save credits
    query = f"founder of startup {startup} LinkedIn Twitter email"
    raw_intel = search_tavily(query, is_news=False)
    if not raw_intel:
        return {"founder_name": "N/A", "linkedin": "#", "x_handle": "N/A", "predicted_email": "N/A"}
        
    prompt = f"""
    Parse this data to extract core profiling info for the primary founder of the startup '{startup}'.
    Return strictly a clean JSON object containing exactly these keys:
    "founder_name", "linkedin", "x_handle", "predicted_email".
    Provide 'Not Found' for any missing value. Do not use markdown wrappers.
    Source Material: {raw_intel}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        return json.loads(response_payload.strip())
    except Exception:
        return {"founder_name": "N/A", "linkedin": "#", "x_handle": "N/A", "predicted_email": "N/A"}

def send_report(final_data: list):
    if not final_data:
        print("[*] Sourcing execution returned zero target entities.")
        return
    html = "<h2>Weekly Sourcing Report</h2><table border='1' cellpadding='10' style='border-collapse: collapse;'>"
    html += "<tr><th>Source Context</th><th>Startup</th><th>Founder Entity</th><th>LinkedIn Profile</th><th>X Handle</th><th>Direct Communication</th></tr>"
    for entry in final_data:
        html += f"<tr><td>{entry['fund']}</td><td>{entry['startup']}</td><td>{entry['founder'].get('founder_name', 'N/A')}</td>"
        html += f"<td><a href='{entry['founder'].get('linkedin', '#')}'>LinkedIn Link</a></td>"
        html += f"<td>{entry['founder'].get('x_handle', 'N/A')}</td><td>{entry['founder'].get('predicted_email', 'N/A')}</td></tr>"
    html += "</table>"
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Automated Market Intelligence: Foundational Pipelines"
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg.attach(MIMEText(html, "html"))
    
    try:
        print("\n[*] Attempting to send email digest...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        print("[+] Email sent successfully.")
    except Exception as e:
        print(f"\n[-] CRITICAL EMAIL FAILURE: {e}")
        print("[-] Your data was NOT lost. It is safely stored in 'sourcing_report.json'.")

def main():
    compiled_intelligence = []
    print("[*] Initializing Targeted Institutional Pipelines...")
    
    for fund in FUNDS_TO_TRACK:
        print(f"\n-> Mapping data channels for {fund}...")
        raw_news = search_tavily(f"{fund} startup funding investment announced", is_news=True, days=7)
        if raw_news:
            startups = extract_startups(fund, raw_news)
            for startup in startups:
                print(f"    [+] Structuring profile: {startup}...")
                founder_profile = enrich_founder_data(startup)
                
                # Append to memory
                compiled_intelligence.append({"fund": fund, "startup": startup, "founder": founder_profile})
                
                # HARD SAVE to disk immediately. 
                save_state_to_json(compiled_intelligence)
                time.sleep(2) 
                
    send_report(compiled_intelligence)
    print("\n[+] Sourcing operation completed cleanly.")

if __name__ == "__main__":
    main()
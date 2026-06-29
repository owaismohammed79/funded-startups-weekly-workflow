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
    model_pool = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
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
    response = tavily_client.search(query=query, search_depth="basic", topic="general")
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
    except Exception:
        return []

def extract_founder_names(startup: str) -> list:
    """Step 1 of the multi-step logic: Find the names first."""
    query = f"'{startup}' startup founder names"
    raw_intel = search_tavily(query)
    if not raw_intel: return []
    
    prompt = f"""
    Extract the names of the founders for the startup '{startup}'.
    Return strictly a JSON array of strings containing their names (e.g., ["Alice Smith", "Bob Jones"]).
    If no names are found, return []. Do not use markdown wrappers.
    Data: {raw_intel}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        return clean_and_parse_json(response_payload)
    except Exception:
        return []

def enrich_specific_founder(startup: str, founder_name: str) -> dict:
    """Step 2 of the multi-step logic: Target the specific individual."""
    query = f"{founder_name} {startup} LinkedIn Twitter"
    raw_intel = search_tavily(query)
    
    prompt = f"""
    Parse this data to find the social profiles for '{founder_name}', founder of '{startup}'.
    Return strictly a valid JSON object with exactly these keys:
    "founder_name": "{founder_name}",
    "linkedin": [Array of strings containing their actual LinkedIn URLs],
    "x_handle": [Array of strings containing their actual X/Twitter URLs]
    
    If URLs are missing, return an empty array []. Do not use markdown wrappers.
    Material:
    {raw_intel}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        data = clean_and_parse_json(response_payload)
        # Type safety: Force strings into arrays if the LLM disobeys the prompt structure
        if isinstance(data.get("linkedin"), str): data["linkedin"] = [data["linkedin"]]
        if isinstance(data.get("x_handle"), str): data["x_handle"] = [data["x_handle"]]
        return data
    except Exception:
        return {"founder_name": founder_name, "linkedin": [], "x_handle": []}

def format_links(links_array: list, label: str) -> str:
    """Safely converts URL arrays to HTML. Destroys dead links and '#' symbols."""
    if not links_array or not isinstance(links_array, list):
        return "N/A"
        
    clean_links = []
    for link in links_array:
        if link and isinstance(link, str):
            link = link.strip()
            # Strict validation: Must contain HTTP to be rendered as an anchor
            if link not in ["", "#", "N/A", "Not Found"] and link.startswith("http"):
                clean_links.append(link)
                
    if not clean_links:
        return "N/A"
        
    # Maps valid URLs to clickable stacked links (e.g., "LinkedIn 1", "LinkedIn 2")
    return "<br>".join([f"<a href='{url}' target='_blank'>{label} {i+1}</a>" for i, url in enumerate(clean_links)])

def send_report(final_data: list):
    if not final_data:
        print("[*] Sourcing engine returned zero entities for this window.")
        return
        
    html = "<h2>Weekly Sourcing Report</h2><table border='1' cellpadding='10' style='border-collapse: collapse;'>"
    html += "<tr><th>Source Context</th><th>Startup</th><th>Founder</th><th>LinkedIn Profile</th><th>X Handle</th></tr>"
    
    for entry in final_data:
        founder = entry.get("founder", {})
        founder_name = founder.get("founder_name", "N/A")
        
        # Route arrays through the safe formatter
        linkedin_html = format_links(founder.get("linkedin", []), "LinkedIn")
        x_html = format_links(founder.get("x_handle", []), "X Profile")
        
        html += f"<tr><td>{entry['fund']}</td><td>{entry['startup']}</td><td>{founder_name}</td>"
        html += f"<td>{linkedin_html}</td><td>{x_html}</td></tr>"
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
        raw_news = search_tavily(f"{fund} recent portfolio companies investments batch")
        
        if raw_news:
            startups = extract_startups(fund, raw_news)
            print(f"    Found Startups: {startups}")
            
            for startup in startups:
                print(f"    [+] Locating founder entities for {startup}...")
                
                # Execution of the multi-step name targeting logic
                founder_names = extract_founder_names(startup)
                
                if not founder_names:
                    print(f"        [-] No explicit founder names extracted for {startup}.")
                    compiled_intelligence.append({
                        "fund": fund, 
                        "startup": startup, 
                        "founder": {"founder_name": "N/A", "linkedin": [], "x_handle": []}
                    })
                    continue
                
                for name in founder_names:
                    print(f"        [+] Extracting social URLs for {name}...")
                    founder_profile = enrich_specific_founder(startup, name)
                    
                    compiled_intelligence.append({
                        "fund": fund, 
                        "startup": startup, 
                        "founder": founder_profile
                    })
                    
                    save_state_to_json(compiled_intelligence)
                    time.sleep(2) 
                
    send_report(compiled_intelligence)
    print("\n[+] Core pipeline run finalized.")

if __name__ == "__main__":
    main()
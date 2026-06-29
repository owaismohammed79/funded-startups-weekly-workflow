import os
import time
import json
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from groq import Groq
from tavily import TavilyClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")

groq_client = Groq(api_key=GROQ_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

# High-signal institutional funds
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
    cleaned = re.sub(r'<think>.*?</think>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'^```json\s*', '', cleaned.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'^```\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)
    return json.loads(cleaned.strip())

def save_state_to_json(data: list, filename: str = "sourcing_report.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    print(f"    [💾] State synchronized cleanly to {filename}")

def generate_with_fallback(prompt: str) -> str:
    """Hierarchical fallback structure maximizing high-TPM models first."""
    model_pool = [
        "meta-llama/llama-4-scout-17b-16e-instruct", 
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile"
    ]
    max_retries = 3
    
    for model in model_pool:
        attempt = 0
        while attempt < max_retries:
            try:
                response = groq_client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": "You are a data extraction assistant. You must output valid JSON only. Do not include your internal reasoning."},
                        {"role": "user", "content": prompt}
                    ],
                    model=model,
                    response_format={"type": "json_object"}, 
                    temperature=0.2
                )
                return response.choices[0].message.content
            
            except Exception as e:
                error_str = str(e).lower()
                
                if "request too large" in error_str or "tpm" in error_str:
                    print(f"    [!] Payload exceeds TPM bucket for {model}. Cascading...")
                    break 
                
                if "tpd" in error_str or "per day" in error_str:
                    print(f"    [!] Groq Daily Token Limit (TPD) hit on {model}. Cascading...")
                    break 
                
                if "429" in error_str or "rate" in error_str:
                    wait_time = 15 * (attempt + 1)
                    print(f"    [⏳] Groq Rolling Limit Hit on {model}. Backing off {wait_time}s...")
                    time.sleep(wait_time)
                    attempt += 1
                    continue
                
                print(f"    [!] Internal API Exception on {model}: {e}")
                break  
                
    return "{}"

def with_retry(max_retries=3, delay=5):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        print(f"[-] Permanent operational blowout in '{func.__name__}': {e}")
                        return None
                    print(f"[!] Network error encountered in '{func.__name__}'. Retrying in {delay}s...")
                    time.sleep(delay)
        return wrapper
    return decorator

@with_retry(max_retries=3, delay=3)
def search_tavily_general(query: str) -> str:
    response = tavily_client.search(query=query, search_depth="basic", max_results=10)
    results = response.get("results", [])
    return "\n---\n".join([f"Title: {r.get('title', 'Unknown')}\nContent: {r.get('content', '')}" for r in results])

@with_retry(max_retries=3, delay=3)
def search_tavily_social(query: str) -> str:
    response = tavily_client.search(
        query=query, 
        search_depth="basic",
        include_domains=["linkedin.com", "twitter.com", "x.com"],
        max_results=5
    )
    results = response.get("results", [])
    return "\n---\n".join([f"URL: {r.get('url', 'Unknown')}\nContent: {r.get('content', '')}" for r in results])

def extract_startups(source_name: str, raw_text: str) -> list:
    if not raw_text or not raw_text.strip(): return []
    
    # NEW: Ruthless Temporal and Stage Kill-Switches
    prompt = f"""
    Analyze this raw tech ecosystem material regarding '{source_name}'.
    
    CRITICAL FILTERING RULES:
    1. TARGET: ONLY extract startups that explicitly raised a Pre-Seed, Seed, or Series A round in 2025 or 2026.
    2. TIME KILL-SWITCH: If the text mentions the seed/early round happened years ago (e.g., 2020, 2022), EXCLUDE IT immediately.
    3. MATURITY KILL-SWITCH: If the text mentions the company later raised a Series B, Series C, Growth Round, or was acquired, EXCLUDE IT completely. 
    
    Return strictly a JSON object with a single key "startups" containing an array of strings (the names).
    If no matches survive these rules, return {{"startups": []}}.
    
    Data:
    {raw_text}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        return clean_and_parse_json(response_payload).get("startups", [])
    except Exception:
        return []

def extract_founder_names(startup: str) -> list:
    query = f"'{startup}' startup founders"
    raw_intel = search_tavily_general(query)
    if not raw_intel: return []
    
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
    query = f"{founder_name} {startup} LinkedIn Twitter"
    raw_intel = search_tavily_social(query)
    if not raw_intel:
        return {"founder_name": founder_name, "linkedin": [], "x_handle": []}
        
    # NEW: Strict /in/ enforcement for LinkedIn profiles
    prompt = f"""
    Parse this data to find the social profiles for '{founder_name}', founder of '{startup}'.
    Return strictly a JSON object with exactly these keys:
    "founder_name": "{founder_name}",
    "linkedin": [Array of strings. MUST be user profiles containing 'linkedin.com/in/'. Exclude '/posts/', '/company/', or articles],
    "x_handle": [Array of strings containing their actual X/Twitter profile URLs]
    
    If valid profile URLs are missing, return empty arrays [].
    
    Material:
    {raw_intel}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        data = clean_and_parse_json(response_payload)
        if isinstance(data.get("linkedin"), str): data["linkedin"] = [data["linkedin"]]
        if isinstance(data.get("x_handle"), str): data["x_handle"] = [data["x_handle"]]
        
        # Python-level double check to destroy bad links before saving
        data["linkedin"] = [link for link in data.get("linkedin", []) if "/in/" in link]
        
        return data
    except Exception:
        return {"founder_name": founder_name, "linkedin": [], "x_handle": []}

def format_links(links_array: list, label: str) -> str:
    if not links_array or not isinstance(links_array, list): return "N/A"
    clean_links = []
    for link in links_array:
        if link and isinstance(link, str):
            link = link.strip()
            if link not in ["", "#", "N/A", "Not Found"] and link.startswith("http"):
                clean_links.append(link)
    if not clean_links: return "N/A"
    return "<br>".join([f"<a href='{url}' target='_blank'>{label} {i+1}</a>" for i, url in enumerate(clean_links)])

def send_report(final_data: list):
    if not final_data:
        print("[*] Sourcing engine returned zero early-stage entities for this window.")
        return
        
    html = "<h2>Weekly Sourcing Report (High-Signal Early Stage Founders)</h2><table border='1' cellpadding='10' style='border-collapse: collapse;'>"
    html += "<tr><th>Source Context</th><th>Startup</th><th>Founders</th><th>LinkedIn Profiles</th><th>X Handles</th></tr>"
    
    for entry in final_data:
        names_list = entry.get("founder_names", [])
        founder_names_str = ", ".join(names_list) if names_list else "N/A"
        
        linkedin_html = format_links(entry.get("linkedin", []), "LinkedIn")
        x_html = format_links(entry.get("x_handle", []), "X Profile")
        
        html += f"<tr><td>{entry['fund']}</td><td>{entry['startup']}</td><td>{founder_names_str}</td>"
        html += f"<td>{linkedin_html}</td><td>{x_html}</td></tr>"
    html += "</table>"
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "High-Signal Sourcing Pipeline: Early Stage Founders"
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg.attach(MIMEText(html, "html"))
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        print("[+] Early-stage intelligence digest deployed successfully.")
    except Exception as e:
        print(f"\n[-] CRITICAL EMAIL FAILURE: {e}")

def main():
    compiled_intelligence = []
    print("[*] Initializing Dynamic Sourcing Pipeline via Groq...")
    
    for fund in FUNDS_TO_TRACK:
        # Query-Level Filter: Enforcing the stage parameter right at the search level to choke off historical data bloat
        # Forces the search engine to prioritize current-year announcements
        search_query = f'"{fund}" ("pre-seed" OR "seed" OR "series A") funding announced 2026'
        print(f"\n-> Fetching high-signal portfolio indicators for {fund}...")
        raw_news = search_tavily_general(search_query)
        
        if raw_news:
            startups = extract_startups(fund, raw_news)
            print(f"    Filtered Early Stage Startups: {startups}")
            
            for startup in startups:
                print(f"    [+] Locating founder entities for {startup}...")
                founder_names = extract_founder_names(startup)
                
                if not founder_names:
                    print(f"        [-] No explicit founder names extracted for {startup}.")
                    compiled_intelligence.append({
                        "fund": fund, 
                        "startup": startup, 
                        "founder_names": ["N/A"], 
                        "linkedin": [], 
                        "x_handle": []
                    })
                    continue
                
                startup_record = {
                    "fund": fund,
                    "startup": startup,
                    "founder_names": [],
                    "linkedin": [],
                    "x_handle": []
                }
                
                for name in founder_names:
                    print(f"        [+] Extracting targeted URLs for {name}...")
                    founder_profile = enrich_specific_founder(startup, name)
                    
                    startup_record["founder_names"].append(founder_profile.get("founder_name", name))
                    startup_record["linkedin"].extend(founder_profile.get("linkedin", []))
                    startup_record["x_handle"].extend(founder_profile.get("x_handle", []))
                
                compiled_intelligence.append(startup_record)
                save_state_to_json(compiled_intelligence)
                
                # Dynamic pacing to prevent hitting rolling minute rate limits
                time.sleep(4) 
                
    send_report(compiled_intelligence)
    print("\n[+] Target pipeline run finalized.")

if __name__ == "__main__":
    main()
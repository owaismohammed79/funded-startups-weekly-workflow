import os
import time
import json
import re
import requests
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone

import gspread
from groq import Groq, APIStatusError
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
DEV_TO_API_KEY = os.environ.get("DEV_TO_API_KEY")
DEV_TO_ORG_ID = os.environ.get("DEV_TO_ORG_ID")
YC_OSS_URL = os.environ.get("YC_OSS_URL")

REQUIRED_ENV_VARS = {
    "TAVILY_API_KEY": TAVILY_API_KEY,
    "GROQ_API_KEY": GROQ_API_KEY,
    "BREVO_API_KEY": BREVO_API_KEY,
    "SENDER_EMAIL": SENDER_EMAIL,
    "SPREADSHEET_ID": SPREADSHEET_ID,
}
missing = [name for name, val in REQUIRED_ENV_VARS.items() if not val]
if missing:
    raise RuntimeError(
        f"Missing required environment variable(s): {', '.join(missing)}. "
        "Set these as GitHub Actions secrets before running."
    )

groq_client = Groq(api_key=GROQ_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

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
MAX_NEW_STARTUPS_PER_RUN = 10

STATE_DIR = "data/state"
LEDGER_PATH = os.path.join(STATE_DIR, "seen_startups.json")
REPORT_PATH = "sourcing_report.json"

MODEL_CASCADE = [
    ("groq", "openai/gpt-oss-120b"),
    ("groq", "openai/gpt-oss-20b"),
    ("groq", "qwen/qwen3.6-27b")
]

MAX_WAIT_THRESHOLD = 30.0

YC_OSS_ALL_URL = YC_OSS_URL
YC_REQUEST_HEADERS = {"User-Agent": "founder-sourcing-pipeline/1.0 (+personal weekly research script)"}
YC_REQUEST_TIMEOUT = 20

_yc_directory_cache = None


def get_yc_directory() -> dict:
    """Loads name->slug map from yc-oss/api (free, unofficial, daily-refreshed mirror
    of YC's own Algolia index). Cached for the life of the process - one fetch per run,
    not per startup. NOTE: this index has slugs/metadata only, no founder data -
    founders come from fetch_yc_founders() below."""
    global _yc_directory_cache
    if _yc_directory_cache is not None:
        return _yc_directory_cache
    try:
        resp = requests.get(YC_OSS_ALL_URL, headers=YC_REQUEST_HEADERS, timeout=YC_REQUEST_TIMEOUT)
        resp.raise_for_status()
        companies = resp.json()
        _yc_directory_cache = {
            normalize_text(c.get("name", "")).lower(): c.get("slug")
            for c in companies if c.get("slug") and c.get("name")
        }
        print(f"    [+] YC directory loaded: {len(_yc_directory_cache)} companies indexed.")
    except Exception as e:
        print(f"    [!] YC directory fetch failed ({e}); YC-direct lookup disabled for this run.")
        _yc_directory_cache = {}
    return _yc_directory_cache


def resolve_yc_slug(startup: str):
    directory = get_yc_directory()
    if not directory:
        return None
    key = normalize_text(startup).lower()
    if key in directory:
        return directory[key]
    root = company_root(startup).lower()
    if not root:
        return None
    for name, slug in directory.items():
        if root == name or root in name:
            return slug
    return None


def fetch_yc_founders(slug: str) -> list:
    """Scrapes YC's own server-rendered company page for its 'Active Founders' block.
    Ground-truth source: no LLM involved, so no hallucination surface at all. Degrades
    to an empty list (never raises) on any structural or network failure so callers can
    fall back to the search+LLM pipeline transparently."""
    url = f"https://www.ycombinator.com/companies/{slug}"
    try:
        resp = requests.get(url, headers=YC_REQUEST_HEADERS, timeout=YC_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []
        html = resp.text
    except Exception as e:
        print(f"        [!] YC page fetch failed for '{slug}': {e}")
        return []

    start_idx = html.find("Active Founders")
    if start_idx == -1:
        return []
    end_idx = len(html)
    for marker in ("Latest News", "YC Photos", "Primary Partner"):
        idx = html.find(marker, start_idx)
        if idx != -1:
            end_idx = min(end_idx, idx)
    section = html[start_idx:end_idx]

    avatar_pattern = re.compile(r'<img[^>]+alt="([^"]+)"[^>]+avatars/', re.IGNORECASE)
    matches = list(avatar_pattern.finditer(section))

    founders = []
    seen_names = set()
    for i, m in enumerate(matches):
        name = normalize_text(m.group(1))
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())
        chunk_end = matches[i + 1].start() if i + 1 < len(matches) else len(section)
        chunk = section[m.end():chunk_end]

        linkedin_match = re.search(r'https://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w\-_%]+/?', chunk, re.IGNORECASE)
        social_match = re.search(
            r'https://(?:twitter|x)\.com/(?!(?:search|hashtag|i|home)(?:/|$))[\w]+/?', chunk, re.IGNORECASE
        )
        founders.append({
            "founder_name": name,
            "linkedin": [linkedin_match.group(0)] if linkedin_match else [],
            "x_handle": [social_match.group(0)] if social_match else [],
        })
    return founders


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split())


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
    print(f"    Ledger updated at {LEDGER_PATH} "
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
    clean_model_id = model.replace("groq/", "")
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = groq_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": "You are a precise data extraction assistant. Output valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                model=clean_model_id,
                response_format={"type": "json_object"},
                temperature=0,
            )
            return response.choices[0].message.content
        except APIStatusError as e:
            if e.status_code == 429:
                wait_time = (attempt + 1) * 5
                print(f"        [!] 429 Rate Limit on {clean_model_id}. Sleeping {wait_time}s...")
                time.sleep(wait_time)
            else:
                raise e
                
    raise Exception(f"Failed {clean_model_id} after {max_retries} rate-limit retries.")

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
    print("    All models in cascade exhausted or failed for this prompt.")
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
def search_tavily_social_structured(query: str, include_domains=None, search_depth: str = "advanced", max_results: int = 5) -> list:
    """Returns structured [{'url':..., 'content':...}] instead of a joined blob.
    Keeping results per-item (rather than concatenating into one string) is what
    lets scoring attribute a snippet to the SPECIFIC url it came from, instead of
    an LLM cross-attributing company mentions from result #3 onto the url in result #1.
    """
    params = {
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
    }
    if include_domains:
        params["include_domains"] = include_domains
    response = tavily_client.search(**params)
    results = response.get("results", [])
    return [{"url": r.get("url", ""), "content": r.get("content", "")} for r in results]


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
        normalized_raw = normalize_text(raw_text)

        for item in data.get("startups", []):
            name = item.get("name")
            quote = item.get("evidence_quote", "")
            normalized_quote = normalize_text(quote)

            if name and normalized_quote and (
                normalized_quote in normalized_raw or normalized_quote[:30] in normalized_raw
            ):
                valid_startups.append(name)
            else:
                print(f"        [-] Hallucination intercepted: Destroying unverified data for '{name}'")

        return valid_startups
    except Exception:
        return []

def _extract_founders_via_llm(startup: str, fund: str, raw_intel: str) -> list:
    """Internal helper: Extracts names ONLY based on the provided search text."""
    prompt = f"""
    Extract the names of the founders/co-founders/CEOs for the startup '{startup}'.
    
    CRITICAL RULES:
    1. DO NOT extract partners, general partners, or founders of the venture capital firm '{fund}' itself.
    2. Only extract individuals who are actual founders, co-founders, or C-level executives of the STARTUP '{startup}'.
    3. Order the extracted names by executive role (e.g., CEO first, CTO second, Co-founders next).
    
    Return strictly a JSON object with a single key "founders" containing an ordered array of strings (their names).
    If no actual startup founders are explicitly verified, return {{"founders": []}}.
    Data: 
    {raw_intel}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        return clean_and_parse_json(response_payload).get("founders", [])
    except Exception:
        return []


def generate_refined_query(startup: str, fund: str, attempt: int, previous_queries: list) -> str:
    """Generates a novel search query if previous attempts failed."""
    prompt = f"""
    Our search for founder names for startup '{startup}' (funded by '{fund}') returned no results.
    Previous failed queries: {previous_queries}

    Generate ONE creative search query string to find the startup's founders.
    STRATEGIES TO TRY:
    - Use VC abbreviations (e.g., 'a16z' instead of 'Andreessen Horowitz', 'YC' for 'Y Combinator').
    - Strip generic corporate words like 'AI', 'Labs', 'App', 'Inc', 'Solutions'.
    - Add context keywords like 'stealth', 'ex-Google', 'pre-seed', 'CEO', 'co-founder'.

    Return strictly a JSON object: {{"search_query": "your refined query here"}}
    """
    try:
        res = generate_with_fallback(prompt)
        return clean_and_parse_json(res).get("search_query", f'"{startup}" founder')
    except Exception:
        # Failsafe fallback
        clean_startup = re.sub(r'\b(App|AI|Solutions|Inc|Corp|Technologies|Labs)\b', '', startup, flags=re.IGNORECASE).strip()
        return f'"{clean_startup}" co-founder {fund}'


def extract_founder_names(startup: str, fund: str, raw_news: str = None) -> list:
    """Master loop: tries the already-paid-for fund-level article text first (free),
    then up to 3 fresh search strategies if that comes up empty."""
    max_attempts = 3
    failed_queries = []

    if raw_news and raw_news.strip():
        names = _extract_founders_via_llm(startup, fund, raw_news)
        if names:
            print(f"        [+] Secured founders from already-fetched fund article (0 extra search credits).")
            return names

    fund_aliases = {
        "Andreessen Horowitz": "Andreessen Horowitz OR a16z",
        "South Park Commons": "South Park Commons OR SPC",
        "Y Combinator": "Y Combinator OR YC"
    }
    fund_query_str = fund_aliases.get(fund, fund)
    clean_startup = re.sub(r'\b(App|AI|Solutions|Inc|Corp|Technologies|Labs)\b', '', startup, flags=re.IGNORECASE).strip()

    for attempt in range(1, max_attempts + 1):
        #Attempt 1
        if attempt == 1:
            query = f'"{startup}" ({fund_query_str}) founder'
        
        #Attempt 2
        elif attempt == 2:
            query = f'"{clean_startup}" startup ({fund_query_str}) founder co-founder'
            print(f"        [~] Attempt {attempt}: Trying cleaned root name query...")
        
        #Attempt 3
        else:
            print(f"        [~] Attempt {attempt}: Generating adaptive search query via LLM...")
            query = generate_refined_query(startup, fund, attempt, failed_queries)

        failed_queries.append(query)
        
        raw_intel = search_tavily_general(query, search_depth="advanced", max_results=6)

        if not raw_intel or not raw_intel.strip():
            print(f"        [-] Attempt {attempt} returned empty search data.")
            continue

        names = _extract_founders_via_llm(startup, fund, raw_intel)
        
        if names:
            if attempt > 1:
                print(f"        [+] Secured founders on attempt {attempt} using query: '{query}'")
            return names
        else:
            print(f"        [-] Attempt {attempt} failed to find valid names in text.")

    #If all 3 loops fail, return empty
    return []

def is_valid_profile(url: str) -> bool:
    """Validates if a URL is a personal LinkedIn profile. Blocks feeds, posts"""
    pattern = r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[\w\-_%]+/?$"
    return bool(re.match(pattern, url, re.IGNORECASE))


GENERIC_STARTUP_WORDS = {"app", "ai", "inc", "corp", "solutions", "labs", "technologies", "hq", "co"}

HIGH_CONFIDENCE_THRESHOLD = 65
MEDIUM_CONFIDENCE_THRESHOLD = 35

SOCIAL_TOXIC_PATTERNS = ("/posts/", "/status/", "/hashtag/", "/search", "?q=")


def company_root(startup: str) -> str:
    """Strips generic corporate suffixes so 'Banza App' -> 'Banza' for substring matching."""
    tokens = [t for t in re.split(r"\s+", startup.strip()) if t.lower() not in GENERIC_STARTUP_WORDS]
    return " ".join(tokens) if tokens else startup.strip()


def slug_name_match_score(url: str, founder_name: str) -> float:
    """Fuzzy-matches the founder's name tokens against the profile URL slug.
    This signal is deliberately independent of any search snippet quality -
    a LinkedIn slug like '/in/john-doe-4a1b2' is strong evidence on its own,
    and doesn't disappear just because Tavily truncated the surrounding text.
    Returns a 0.0-1.0 ratio via difflib (stdlib, no extra dependency).
    """
    slug_match = re.search(r"/in/([\w\-]+)/?$", url, re.IGNORECASE)
    if not slug_match:
        return 0.0
    slug_tokens = re.sub(r"[\d_%]+", " ", slug_match.group(1)).replace("-", " ").strip()
    name_norm = normalize_text(founder_name).lower()
    slug_norm = normalize_text(slug_tokens).lower()
    if not slug_norm or not name_norm:
        return 0.0
    return SequenceMatcher(None, name_norm, slug_norm).ratio()


def snippet_mentions_company(snippet: str, startup: str) -> bool:
    root = company_root(startup).lower()
    if not root:
        return False
    return root in normalize_text(snippet).lower()


def is_toxic_link(url: str) -> bool:
    return any(p in url.lower() for p in SOCIAL_TOXIC_PATTERNS)


def score_candidates(candidates: dict, founder_name: str, startup: str, seen_in_both_queries: set) -> list:
    """candidates: {url: best_snippet}. Returns list of dicts sorted by score desc."""
    scored = []
    for url, snippet in candidates.items():
        if is_toxic_link(url) or not is_valid_profile_any(url):
            continue
        score = 0.0
        name_sim = slug_name_match_score(url, founder_name)
        score += name_sim * 40  # Signal 1: slug<->name fuzzy match, snippet-independent
        if snippet_mentions_company(snippet, startup):
            score += 35          # Signal 2: company root in THIS result's own snippet
        if url in seen_in_both_queries:
            score += 25           # Signal 3: corroborated by two independently-phrased queries
        scored.append({"url": url, "snippet": snippet, "score": round(score, 1), "name_sim": round(name_sim, 2)})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


def is_valid_profile_any(url: str) -> bool:
    """Accepts LinkedIn /in/ profiles or X/Twitter handle-root profiles; blocks feeds/posts/search."""
    if is_valid_profile(url):
        return True
    x_pattern = r"^https?://(www\.)?(twitter|x)\.com/(?!(search|hashtag|i|home)(/|$))[\w]+/?$"
    return bool(re.match(x_pattern, url, re.IGNORECASE))


def arbitrate_medium_confidence(founder_name: str, startup: str, candidates: list) -> dict:
    """LLM picks from a FIXED enumerated list of pre-scored candidates - it cannot
    fabricate a new URL because it is only allowed to select an index, never emit
    freeform link text back into the accepted result."""
    if len(candidates) == 1:
        return candidates[0]

    menu = "\n".join(
        f"{i}: url={c['url']} | name_similarity={c['name_sim']} | snippet=\"{c['snippet'][:200]}\""
        for i, c in enumerate(candidates)
    )
    prompt = f"""
    Multiple candidate social profile URLs were found for founder '{founder_name}' of startup '{startup}'.
    Choose the ONE candidate index that is genuinely this person, or -1 if none qualify.
    Do not invent a URL. Only return an index that appears in the menu below.

    Menu:
    {menu}

    Return strictly JSON: {{"chosen_index": <int>}}
    """
    response_payload = generate_with_fallback(prompt)
    try:
        idx = clean_and_parse_json(response_payload).get("chosen_index", -1)
        if isinstance(idx, int) and 0 <= idx < len(candidates):
            return candidates[idx]
    except Exception:
        pass
    return None


def enrich_specific_founder(startup: str, founder_name: str) -> dict:
    """Tiered, multi-signal founder verification.
    Two independently-phrased queries feed a deterministic scorer (Stage A+B);
    the LLM is only invoked as a bounded arbiter over medium-confidence ties
    (Stage C), never as the URL source of truth.
    """
    result = {"founder_name": founder_name, "linkedin": [], "x_handle": [], "confidence": "rejected"}

    company_query = search_tavily_social_structured(
        f'"{founder_name}" "{startup}"',
        include_domains=["linkedin.com", "twitter.com", "x.com"],
        search_depth="advanced",
        max_results=5,
    )
    name_query = search_tavily_social_structured(
        f'"{founder_name}" linkedin',
        include_domains=["linkedin.com", "twitter.com", "x.com"],
        search_depth="advanced",
        max_results=5,
    )

    company_urls = {r["url"] for r in company_query if r["url"]}
    name_urls = {r["url"] for r in name_query if r["url"]}
    overlap = company_urls & name_urls

    candidates_raw = {}
    for r in company_query + name_query:
        if r["url"] and r["url"] not in candidates_raw:
            candidates_raw[r["url"]] = r["content"]

    if not candidates_raw:
        return result

    scored = score_candidates(candidates_raw, founder_name, startup, overlap)
    if not scored:
        return result

    high = [c for c in scored if c["score"] >= HIGH_CONFIDENCE_THRESHOLD]
    medium = [c for c in scored if MEDIUM_CONFIDENCE_THRESHOLD <= c["score"] < HIGH_CONFIDENCE_THRESHOLD]

    if not high and not medium:
        print(f"        [~] Best candidate for {founder_name} scored {scored[0]['score']}/100 "
              f"(needs >= {MEDIUM_CONFIDENCE_THRESHOLD}): {scored[0]['url']}")

    accepted = []
    if high:
        accepted = high
        result["confidence"] = "high"
    elif medium:
        chosen = arbitrate_medium_confidence(founder_name, startup, medium)
        if chosen:
            accepted = [chosen]
            result["confidence"] = "medium"

    for c in accepted:
        if is_valid_profile(c["url"]):
            result["linkedin"].append(c["url"])
        else:
            result["x_handle"].append(c["url"])

    return result

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


def format_links_markdown(links_array: list, label: str) -> str:
    """Formats links cleanly for Markdown tables"""
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
    return " <br> ".join(f"[{label} {i+1}]({url})" for i, url in enumerate(clean_links))


def generate_markdown_report(final_data: list) -> str:
    """Converts the extracted data into Markdown table."""
    md = "### Autonomous OSINT Intelligence Report\n\n"
    md += "Here are the verified early-stage startup founders extracted by our autonomous AI pipeline this week.\n\n"
    md += "| Source Context | Startup | Founders | LinkedIn Profiles | X Handles |\n"
    md += "| :--- | :--- | :--- | :--- | :--- |\n"

    for entry in final_data:
        names_list = entry.get("founder_names", [])
        founder_names_str = ", ".join(names_list) if names_list else "N/A"
        linkedin_md = format_links_markdown(entry.get("linkedin", []), "LinkedIn")
        x_md = format_links_markdown(entry.get("x_handle", []), "X Profile")

        fund = entry.get("fund", "N/A")
        startup = entry.get("startup", "N/A")

        md += f"| {fund} | {startup} | {founder_names_str} | {linkedin_md} | {x_md} |\n"

    md += "\n---\n*Automated weekly intelligence report powered by Groq, Tavily, and GitHub Actions.*"
    return md


def publish_to_devto(title: str, markdown_content: str):
    if not DEV_TO_API_KEY:
        print("    [-] DEV_TO_API_KEY missing. Skipping DEV.to publish.")
        return None

    url = "https://dev.to/api/articles"
    headers = {
        "api-key": DEV_TO_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "article": {
            "title": title,
            "published": True,
            "body_markdown": markdown_content,
            "tags": ["startups", "ai", "opensource"],
        }
    }

    if DEV_TO_ORG_ID:
        payload["article"]["organization_id"] = int(DEV_TO_ORG_ID)

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code in (200, 201):
            post_url = response.json().get("url")
            print(f"    [+] Successfully published to DEV.to! Live at: {post_url}")
            return post_url
        else:
            print(f"    [-] DEV.to API Error: {response.text}")
    except Exception as e:
        print(f"    [-] Network error publishing to DEV.to: {e}")
    return None


def fetch_form_subscribers() -> list:
    try:
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

def cache_latest_report_in_sheet(html_content: str):
    creds_file = "credentials.json"
    if not os.path.exists(creds_file):
        print(f"    [-] Error: '{creds_file}' missing. Cannot update sheet cache.")
        return

    if not SPREADSHEET_ID:
        print("    [-] Error: SPREADSHEET_ID env var is missing.")
        return

    try:
        gc = gspread.service_account(filename=creds_file)
        sh = gc.open_by_key(SPREADSHEET_ID)
        
        try:
            worksheet = sh.worksheet("LatestReport")
        except gspread.WorksheetNotFound:
            worksheet = sh.add_worksheet(title="LatestReport", rows="5", cols="2")

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        worksheet.clear()
        worksheet.update(range_name='A1', values=[[today_str]])
        worksheet.update(range_name='A2', values=[[html_content]])
        print(f"    [+] Successfully cached latest report HTML in Google Sheet! ({today_str})")
    except Exception as e:
        print(f"    [-] Failed to cache report in Sheet: {e}")


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
    table_html += "<tr><th>Source Context</th><th>Startup</th><th>Founders</th><th>LinkedIn Profiles</th><th>X Handles</th><th>Confidence</th></tr>"

    for entry in final_data:
        names_list = entry.get("founder_names", [])
        founder_names_str = ", ".join(names_list) if names_list else "N/A"
        linkedin_html = format_links(entry.get("linkedin", []), "LinkedIn")
        x_html = format_links(entry.get("x_handle", []), "X Profile")
        confidence_list = entry.get("confidence", [])
        confidence_str = ", ".join(confidence_list) if confidence_list else "N/A"

        table_html += f"<tr><td>{entry['fund']}</td><td>{entry['startup']}</td><td>{founder_names_str}</td>"
        table_html += f"<td>{linkedin_html}</td><td>{x_html}</td><td>{confidence_str}</td></tr>"
    table_html += "</table>"

    cache_latest_report_in_sheet(table_html)

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

        fund_processed_count = 0

        for startup in startups:
            if len(compiled_intelligence) >= MAX_NEW_STARTUPS_PER_RUN or fund_processed_count >= 3:
                break

            key = startup.lower()
            if key in seen:
                print(f"    [!] Skipping {startup} - already reported on {seen[key]}.")
                continue
            if key in newly_seen_this_run:
                continue

            newly_seen_this_run.add(key)

            yc_founders = []
            if fund == "Y Combinator":
                slug = resolve_yc_slug(startup)
                if slug:
                    print(f"    [+] Resolved YC slug '{slug}' for {startup} - trying ground-truth lookup...")
                    yc_founders = fetch_yc_founders(slug)
                    if yc_founders:
                        print(f"        [+] Got {len(yc_founders)} founder(s) directly from YC's own page "
                              f"(0 Tavily/Groq calls spent).")
                else:
                    print(f"    [~] No YC slug match for '{startup}' - falling back to search pipeline.")

            if yc_founders:
                startup_record = {
                    "fund": fund,
                    "startup": startup,
                    "founder_names": [],
                    "linkedin": [],
                    "x_handle": [],
                    "confidence": [],
                }
                for f in yc_founders[:3]:
                    if not (f.get("linkedin") or f.get("x_handle")):
                        continue
                    startup_record["founder_names"].append(f["founder_name"])
                    startup_record["linkedin"].extend(f.get("linkedin", []))
                    startup_record["x_handle"].extend(f.get("x_handle", []))
                    startup_record["confidence"].append("verified")

                if startup_record["founder_names"]:
                    fund_processed_count += 1
                    compiled_intelligence.append(startup_record)
                    save_state_to_json(compiled_intelligence)
                    continue

            print(f"    [+] Locating founder entities for {startup}...")
            founder_names = extract_founder_names(startup, fund, raw_news=raw_news)

            if not founder_names:
                print(f"        [-] No explicit founder names extracted for {startup}.")
                compiled_intelligence.append({
                    "fund": fund,
                    "startup": startup,
                    "founder_names": ["N/A"],
                    "linkedin": [],
                    "x_handle": [],
                })
                save_state_to_json(compiled_intelligence)
                continue

            fund_processed_count += 1

            startup_record = {
                "fund": fund,
                "startup": startup,
                "founder_names": [],
                "linkedin": [],
                "x_handle": [],
                "confidence": [],
            }

            verified_count = 0

            for name in founder_names:
                if verified_count >= 3:
                    print(f"        [+] Successfully verified 3 founders for {startup}. Moving on")
                    break

                print(f"        [+] Extracting targeted URLs for {name}...")
                founder_profile = enrich_specific_founder(startup, name)
                tier = founder_profile.get("confidence", "rejected")

                if tier in ("high", "medium") and (founder_profile.get("linkedin") or founder_profile.get("x_handle")):
                    startup_record["founder_names"].append(founder_profile.get("founder_name", name))
                    startup_record["linkedin"].extend(founder_profile.get("linkedin", []))
                    startup_record["x_handle"].extend(founder_profile.get("x_handle", []))
                    startup_record["confidence"].append(tier)
                    verified_count += 1
                    print(f"        [+] Accepted {name} at '{tier}' confidence.")
                else:
                    print(f"        [-] Rejected: no candidate cleared the confidence threshold for {name}.")

                time.sleep(4)

            compiled_intelligence.append(startup_record)
            save_state_to_json(compiled_intelligence)
            time.sleep(3)

    today_iso = datetime.now(timezone.utc).date().isoformat()
    for entry in compiled_intelligence:
        seen[entry["startup"].lower()] = today_iso
    save_ledger(ledger)

    send_report(compiled_intelligence)

    if compiled_intelligence:
        today_formatted = datetime.now(timezone.utc).strftime("%b %d, %Y")
        report_title = f"Weekly Startup Intel: YC & a16z Sourcing Drop ({today_formatted})"
        markdown_body = generate_markdown_report(compiled_intelligence)
        
        print("\n[*] Publishing weekly intelligence report to Hashnode...")
        publish_to_devto(report_title, markdown_body)

    print(f"\n[+] Target pipeline run finalized. {len(compiled_intelligence)} new startup(s) reported.")


if __name__ == "__main__":
    main()
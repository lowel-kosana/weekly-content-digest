import os
import sys
import argparse
import json
import re
import html
import time
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Set default encoding to UTF-8
sys.stdout.reconfigure(encoding='utf-8')

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Find and load the config/.env file
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
WORKSPACE_DIR = os.path.dirname(SKILL_DIR)

env_path = os.path.join(WORKSPACE_DIR, "research-meeting-skill", "config", ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    load_dotenv()

try:
    from supabase import create_client, Client
    import google.generativeai as genai
except ImportError as e:
    logger.error(f"Missing dependency. Run 'pip install supabase google-generativeai python-dotenv'. Detail: {e}")
    sys.exit(1)

# --- Module-level Gemini configuration ---
_GEMINI_CONFIGURED = False

def _ensure_gemini_configured():
    """Configure the Gemini API client once at module level."""
    global _GEMINI_CONFIGURED
    if _GEMINI_CONFIGURED:
        return
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        raise RuntimeError("Missing GEMINI_API_KEY environment variable.")
    genai.configure(api_key=gemini_key)
    _GEMINI_CONFIGURED = True

# --- Retry helper ---
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds

def _call_gemini_with_retry(model, prompt: str, generation_config: dict = None) -> object:
    """Call Gemini API with exponential backoff retry on transient failures."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = model.generate_content(prompt, generation_config=generation_config)
            try:
                usage = response.usage_metadata
                logger.info(f"Token Usage: {usage.prompt_token_count} in, {usage.candidates_token_count} out.")
            except AttributeError:
                logger.debug("Token usage metadata not available for this response.")
            return response
        except Exception as e:
            if attempt == MAX_RETRIES:
                logger.error(f"Gemini API call failed after {MAX_RETRIES} attempts: {e}")
                raise
            wait_time = RETRY_BACKOFF_BASE ** attempt
            logger.warning(f"Gemini API attempt {attempt}/{MAX_RETRIES} failed: {e}. Retrying in {wait_time}s...")
            time.sleep(wait_time)


def get_supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY environment variables.")
    return create_client(url, key)

def _repair_json(text: str) -> str:
    """Attempt to repair common LLM JSON malformations."""
    # Remove trailing commas before closing braces/brackets
    text = re.sub(r',\s*([}\]])', r'\1', text)
    # Fix unescaped newlines inside JSON string values
    # (match content between quotes and escape literal newlines)
    text = re.sub(r'(?<=: ")(.*?)(?=")', lambda m: m.group(0).replace('\n', '\\n'), text, flags=re.DOTALL)
    return text

def _safe_json_loads(text: str) -> dict | None:
    """Try parsing JSON, with a repair fallback on failure."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Initial JSON parse failed, attempting repair...")
        try:
            repaired = _repair_json(text)
            return json.loads(repaired)
        except json.JSONDecodeError as e:
            logger.error(f"JSON repair also failed: {e}")
            return None

def clean_json_markdown(text: str) -> str:
    cleaned = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```html\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()

def generate_core_digest_with_llm(grouped_chunks: dict) -> dict:
    _ensure_gemini_configured()
    model = genai.GenerativeModel("gemini-2.5-flash")
    
    # Prepare text for each group
    group_texts = {}
    for topic in ["markets/macro", "watchlist/serial-acquirers", "watchlist/peptides", "watchlist/Generic"]:
        chunks = grouped_chunks.get(topic, [])
        text = ""
        for c in chunks:
            sender = c.get("metadata", {}).get("sender", "Unknown Source")
            date = c.get("published_at", "Unknown Date")
            text += f"\n--- SOURCE: {sender} ({date}) ---\n{c.get('content', '')}\n"
        group_texts[topic] = text if text else "No updates this week."
        
    prompt = f"""
    You are an expert financial analyst and product strategist at Kasona. 
    You need to analyze the weekly content chunks from various newsletters and generate high-quality summaries.
    
    Here is the raw data from this week's content grouped by topic:
    
    ### 1. OVERALL MARKET SENTIMENT (markets/macro)
    {group_texts['markets/macro']}
    
    ### 2. WATCHLIST: SERIAL ACQUIRERS (watchlist/serial-acquirers)
    {group_texts['watchlist/serial-acquirers']}
    
    ### 3. WATCHLIST: PEPTIDES (watchlist/peptides)
    {group_texts['watchlist/peptides']}
    
    ### 4. WATCHLIST: GENERIC (watchlist/Generic)
    {group_texts['watchlist/Generic']}
    
    Generate summaries for each section following these instructions:
    - market_sentiment: Concise bullet points summarizing the overall macro outlook, market direction, inflation, interest rates, or economic data. ONLY extract the most important, highly useful, actionable insights since the end user is a trader. Ignore generic fluff.
    - watchlist_serial_acquirers: Concise bullet points summarizing updates, earnings, or acquisitions for companies like Constellation Software, Lifco, etc. ONLY extract the most important, highly useful, actionable insights since the end user is a trader.
    - watchlist_peptides: Concise bullet points summarizing news, shortages, or expansions in the obesity/peptide market (Eli Lilly, Novo Nordisk, etc.). ONLY extract the most important, highly useful, actionable insights since the end user is a trader.
    - watchlist_generic: Concise bullet points summarizing other general investing/company updates. ONLY extract the most important, highly useful, actionable insights since the end user is a trader.
    
    IMPORTANT RULE:
    For EVERY bullet point generated, you MUST append the exact sender name in brackets at the very end of the sentence. 
    Example: "Constellation Software made a minor acquisition in Germany. [Serial Acquirers Weekly]"
    
    IMPORTANT FORMAT RULE:
    Return each section's value as a JSON ARRAY of strings, where each string is ONE bullet point.
    Do NOT return a single string with newlines. Return an array like: ["Point 1 [Source]", "Point 2 [Source]"]
    
    If any section has no new data (the input text was "No updates this week."), return an empty array [] for that section.
    
    You MUST return the output in JSON format with exactly these keys:
    {{
      "dashboard": {{
        "bias": "Short 2-3 word summary of overall market bias (e.g. Cautiously Bullish)",
        "top_theme": "Short 3-5 word summary of the most prominent theme",
        "risk_signal": "Short 3-5 word summary of the biggest market risk"
      }},
      "market_sentiment": ["bullet 1 [Source]", "bullet 2 [Source]"],
      "watchlist_serial_acquirers": ["bullet 1 [Source]", "bullet 2 [Source]"],
      "watchlist_peptides": ["bullet 1 [Source]", "bullet 2 [Source]"],
      "watchlist_generic": ["bullet 1 [Source]", "bullet 2 [Source]"]
    }}
    
    Return ONLY valid JSON.
    """
    
    logger.info("Generating core digest via Gemini API...")
    response = _call_gemini_with_retry(model, prompt, generation_config={"response_mime_type": "application/json"})
        
    cleaned_text = clean_json_markdown(response.text)
    result = _safe_json_loads(cleaned_text)
    if result is None:
        logger.error(f"Failed to parse core digest JSON. Raw output (first 500 chars):\n{cleaned_text[:500]}")
        return {
            "market_sentiment": [],
            "watchlist_serial_acquirers": [],
            "watchlist_peptides": [],
            "watchlist_generic": []
        }
    return result

def generate_creator_insights_with_llm(grouped_chunks: dict) -> dict:
    _ensure_gemini_configured()
    model = genai.GenerativeModel("gemini-2.5-flash")
    
    # Prepare text for creator groups exclusively
    group_texts = {}
    for topic in ["ai_product_inspiration", "youtube_strategists"]:
        chunks = grouped_chunks.get(topic, [])
        text = ""
        for c in chunks:
            sender = c.get("metadata", {}).get("sender", "Unknown Source")
            date = c.get("published_at", "Unknown Date")
            text += f"\n--- SOURCE: {sender} ({date}) ---\n{c.get('content', '')}\n"
        group_texts[topic] = text if text else "No updates this week."
        
    prompt = f"""
    You are an expert product strategist and analyst at Kasona. 
    You need to analyze content specifically from a curated list of creators to extract highly targeted insights.
    DO NOT hallucinate creators or information that is not present in the text provided.
    
    ### 1. AI PRODUCT INSPIRATION
    {group_texts['ai_product_inspiration']}
    
    ### 2. YOUTUBE STRATEGY INSIGHTS
    {group_texts['youtube_strategists']}
    
    Generate JSON outputs following these strict instructions:
    - ai_product_inspiration: Look ONLY at the content provided under '### 1. AI PRODUCT INSPIRATION'. Return a JSON object where the keys are the EXACT names of the content creators from the source blocks, and the values are a JSON array of string bullet points detailing specific AI tool features, UI ideas, workflow automations, or technical insights. ONLY extract the most important, highly useful, actionable insights since the end user is a trader. If there is no insight, omit the creator.
    - youtube_strategists: Look ONLY at the content provided under '### 2. YOUTUBE STRATEGY INSIGHTS'. Return a JSON object where the keys are the EXACT names of the content creators from the source blocks, and the values are a JSON array of string bullet points extracting the most important, highly useful, actionable insights since the end user is a trader. Do not over-summarize; preserve the high-signal details from the text. If there is no insight, omit the creator.
    
    You MUST return the output in JSON format with exactly these keys:
    {{
      "ai_product_inspiration": {{"Creator A": ["Point 1", "Point 2"]}},
      "youtube_strategists": {{"Creator B": ["Point 1", "Point 2"]}}
    }}
    
    Return ONLY valid JSON.
    """
    
    logger.info("Generating creator insights via Gemini API...")
    response = _call_gemini_with_retry(model, prompt, generation_config={"response_mime_type": "application/json"})
        
    cleaned_text = clean_json_markdown(response.text)
    result = _safe_json_loads(cleaned_text)
    if result is None:
        logger.error(f"Failed to parse creator insights JSON. Raw output (first 500 chars):\n{cleaned_text[:500]}")
        return {
            "ai_product_inspiration": {},
            "youtube_strategists": {}
        }
    return result

def build_html_report(summaries: dict) -> str:
    # Get current date in a pretty format
    date_str = datetime.now().strftime("%B %d, %Y")
    
    # Helper to resolve source_url or content_html for a sender
    def get_source_info(sender, topic_key, topic_sources_dict):
        sources = topic_sources_dict.get(topic_key, [])
        for s in sources:
            if s.get("sender") == sender:
                return s.get("source_url"), s.get("content_html")
        return None, None

    def process_inline_sources(content: str, topic_key=None, topic_sources_dict=None) -> str:
        """Replace ALL [Source Name] tags in a string with styled HTML links or badges."""
        if not topic_key or not topic_sources_dict:
            # Still style the source tags even without link info
            def _style_tag(m):
                sender_name = html.escape(m.group(1))
                return f'<span style="font-size: 12px; color: #64748b; font-weight: 600;">[{sender_name}]</span>'
            return re.sub(r'\[([^\[\]]+)\]', _style_tag, content)
        
        def _replace_source(m):
            sender_name = m.group(1)
            source_url, _ = get_source_info(sender_name, topic_key, topic_sources_dict)
            escaped_name = html.escape(sender_name)
            if source_url:
                return f'<a href="{html.escape(source_url)}" target="_blank" style="font-size: 12px; color: #FF9627; text-decoration: none; font-weight: 600;">[{escaped_name} ↗]</a>'
            else:
                return f'<span style="font-size: 12px; color: #64748b; font-weight: 600;">[{escaped_name}]</span>'
        
        return re.sub(r'\[([^\[\]]+)\]', _replace_source, content)

    # Render a dictionary of creator arrays into hardcoded HTML
    def format_grouped_creator_bullets(data_dict, creator_meta) -> str:
        if not data_dict or not isinstance(data_dict, dict):
            return "<p class='no-updates'>No updates this week.</p>"
            
        html_out = ""
        first = True
        for creator, bullets in data_dict.items():
            if not bullets or not isinstance(bullets, list):
                continue
            
            # Filter out empty strings just in case
            valid_bullets = [b for b in bullets if str(b).strip()]
            if not valid_bullets:
                continue
                
            if not first:
                html_out += '<div style="height: 1px; background-color: #e2e8f0; margin: 24px 0;"></div>\n'
            first = False
            
            meta = creator_meta.get(creator, {})
            source_url = meta.get("source_url")
            escaped_creator = html.escape(creator)
            
            html_out += f'<div style="display: flex; align-items: center; justify-content: space-between; margin: 0 0 8px 0;">\n'
            html_out += f'<p style="font-size: 16px; font-weight: 600; color: #0f172a; margin: 0;">{escaped_creator}:</p>\n'
            if source_url:
                html_out += f'<a href="{html.escape(source_url)}" target="_blank" style="font-size: 12px; color: #FF9627; text-decoration: none; padding: 4px 8px; border: 1px solid #e2e8f0; border-radius: 6px; background-color: #f1f5f9;">Read Source ↗</a>\n'
            html_out += '</div>\n'
            
            html_out += '<ul style="margin: 4px 0 12px 0; padding-left: 24px; color: #475569;">\n'
            for b in valid_bullets:
                clean_b = html.escape(str(b).lstrip('-* ').strip())
                html_out += f'<li style="margin-bottom: 6px; line-height: 1.6; font-size: 14px;">{clean_b}</li>\n'
            html_out += '</ul>\n'
            
        return html_out if html_out else "<p class='no-updates'>No updates this week.</p>"

    # Render markdown-like bullet points or list of strings to HTML
    def format_text(text, topic_key=None, topic_sources_dict=None) -> str:
        if not text:
            return "<p class='no-updates'>No updates this week.</p>"

        if isinstance(text, list):
            if not text:
                return "<p class='no-updates'>No updates this week.</p>"
            html_bullets = ['<ul style="margin: 4px 0 12px 0; padding-left: 24px; color: #475569;">']
            for item in text:
                item_str = str(item).strip()
                if item_str:
                    item_str = item_str.lstrip('-* ').strip()
                    item_str = html.escape(item_str)
                    item_str = process_inline_sources(item_str, topic_key, topic_sources_dict)
                    html_bullets.append(f'<li style="margin-bottom: 6px; line-height: 1.6; font-size: 14px;">{item_str}</li>')
            html_bullets.append('</ul>')
            return '\n'.join(html_bullets)
            
        text_str = str(text).strip()
        if text_str in ("No updates this week.", "Failed to parse.", ""):
            return "<p class='no-updates'>No updates this week.</p>"
            
        # Convert markdown bullets to HTML list
        lines = text_str.split('\n')
        html_bullets = []
        in_list = False
        
        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith('-') or line_str.startswith('*'):
                if not in_list:
                    html_bullets.append('<ul style="margin: 4px 0 12px 0; padding-left: 24px; color: #475569;">')
                    in_list = True
                content = html.escape(line_str.lstrip('-* ').strip())
                content = process_inline_sources(content, topic_key, topic_sources_dict)
                html_bullets.append(f'<li style="margin-bottom: 6px; line-height: 1.6; font-size: 14px;">{content}</li>')
            else:
                if in_list:
                    html_bullets.append('</ul>')
                    in_list = False
                line_str = html.escape(line_str)
                line_str = process_inline_sources(line_str, topic_key, topic_sources_dict)
                html_bullets.append(f'<p style="color: #475569; line-height: 1.6; font-size: 14px; margin: 8px 0;">{line_str}</p>')
        
        if in_list:
            html_bullets.append('</ul>')
            
        return '\n'.join(html_bullets)

    topic_sources = summaries.get("_topic_sources", {})
    
    # Extract Dashboard Data
    dashboard_data = summaries.get("dashboard", {})
    bias = html.escape(str(dashboard_data.get("bias", "N/A")))
    top_theme = html.escape(str(dashboard_data.get("top_theme", "N/A")))
    risk_signal = html.escape(str(dashboard_data.get("risk_signal", "N/A")))
    
    dashboard_html = f"""
    <div class="summary-grid">
        <div class="summary-card bias">
            <div class="summary-label">Market Bias</div>
            <div class="summary-value">{bias}</div>
        </div>
        <div class="summary-card theme">
            <div class="summary-label">Top Theme</div>
            <div class="summary-value">{top_theme}</div>
        </div>
        <div class="summary-card risk">
            <div class="summary-label">Risk Signal</div>
            <div class="summary-value">{risk_signal}</div>
        </div>
    </div>
    """
    
    market_sentiment_html = format_text(summaries.get("market_sentiment", ""), "markets/macro", topic_sources)
    watchlist_serial_acquirers_html = format_text(summaries.get("watchlist_serial_acquirers", ""), "watchlist/serial-acquirers", topic_sources)
    watchlist_peptides_html = format_text(summaries.get("watchlist_peptides", ""), "watchlist/peptides", topic_sources)
    watchlist_generic_html = format_text(summaries.get("watchlist_generic", ""), "watchlist/Generic", topic_sources)

    ai_product_inspiration_html = format_grouped_creator_bullets(summaries.get("ai_product_inspiration", {}), summaries.get("_creator_meta", {}))
    youtube_strategists_html = format_grouped_creator_bullets(summaries.get("youtube_strategists", {}), summaries.get("_creator_meta", {}))

    html_report = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kasona Weekly Digest - {date_str}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Outfit:wght@500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body {{
            margin: 0;
            padding: 0;
            background-color: #fafafa;
            font-family: 'Outfit', 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            color: #0f172a;
            -webkit-font-smoothing: antialiased;
        }}
        .container {{
            max-width: 680px;
            margin: 0 auto;
            padding: 40px 20px;
        }}
        
        /* Dashboard CSS */
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 32px;
        }}
        .summary-card {{
            background-color: #ffffff;
            border-radius: 12px;
            padding: 16px;
            border: 1px solid #e2e8f0;
            position: relative;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        }}
        .summary-card.bias {{ border-left: 4px solid #FF9627; }}
        .summary-card.theme {{ border-left: 4px solid #95DDFA; }}
        .summary-card.risk {{ border-left: 4px solid hsl(342, 90%, 67%); }}
        .summary-label {{ font-size: 10px; text-transform: uppercase; color: #64748b; margin-bottom: 4px; font-weight: 600; }}
        .summary-value {{ font-size: 16px; font-weight: 700; color: #0f172a; }}

        .header {{
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 16px;
            padding: 40px 32px;
            margin-bottom: 32px;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.05);
            text-align: left;
            position: relative;
            overflow: hidden;
        }}
        .header::after {{
            content: '';
            position: absolute;
            top: -50%;
            right: -20%;
            width: 300px;
            height: 300px;
            background: radial-gradient(circle, rgba(255, 150, 39, 0.1) 0%, rgba(255, 255, 255, 0) 70%);
            border-radius: 50%;
        }}
        .header h1 {{
            margin: 0;
            font-size: 32px;
            font-weight: 800;
            letter-spacing: -0.5px;
            background: linear-gradient(135deg, hsl(33, 100%, 58%), hsl(342, 90%, 67%), hsl(195, 91%, 78%));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        .header .date {{
            margin-top: 8px;
            font-size: 14px;
            color: #64748b;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 1px;
            font-family: 'Inter', sans-serif;
        }}
        .card {{
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 16px;
            padding: 32px;
            margin-bottom: 32px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05);
        }}
        .card-title-container {{
            display: flex;
            align-items: center;
            margin-bottom: 20px;
            border-bottom: 1px solid #e2e8f0;
            padding-bottom: 12px;
        }}
        .card-icon {{
            font-size: 20px;
            margin-right: 12px;
        }}
        .card-title {{
            margin: 0;
            font-size: 20px;
            font-weight: 700;
            color: #FF9627;
            letter-spacing: -0.3px;
        }}
        .sentiment-card .card-title {{
            color: #95DDFA; /* secondary cyan */
        }}
        .inspiration-card .card-title {{
            color: hsl(342, 90%, 67%); /* pink from gradient */
        }}
        .watchlist-card .card-title {{
            color: #FF9627; /* primary orange */
        }}
        .watchlist-section {{
            margin-bottom: 24px;
        }}
        .watchlist-section:last-child {{
            margin-bottom: 0;
        }}
        .watchlist-section-header {{
            font-size: 16px;
            font-weight: 600;
            color: #0f172a;
            margin: 0 0 12px 0;
            display: flex;
            align-items: center;
        }}
        .watchlist-badge {{
            background-color: #f1f5f9;
            color: #475569;
            font-size: 11px;
            font-weight: 500;
            padding: 2px 8px;
            border-radius: 12px;
            margin-left: 10px;
            font-family: 'Inter', sans-serif;
        }}
        .no-updates {{
            color: #64748b;
            font-style: italic;
            margin: 8px 0;
            font-size: 15px;
        }}
        .footer {{
            text-align: center;
            margin-top: 48px;
            font-family: 'Inter', sans-serif;
            font-size: 12px;
            color: #475569;
            border-top: 1px solid #e2e8f0;
            padding-top: 24px;
        }}
        .footer p {{
            margin: 4px 0;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Kasona Weekly Digest</h1>
            <div class="date">{date_str}</div>
        </div>

        {dashboard_html}

        <!-- SECTION 1: MARKET SENTIMENT -->
        <div class="card sentiment-card">
            <div class="card-title-container">
                <span class="card-icon">📈</span>
                <h2 class="card-title">Overall Market Sentiment</h2>
            </div>
            {market_sentiment_html}
        </div>

        <!-- SECTION 2: WATCHLISTS -->
        <div class="card watchlist-card">
            <div class="card-title-container">
                <span class="card-icon">🎯</span>
                <h2 class="card-title">Watchlist Summaries</h2>
            </div>
            
            <div class="watchlist-section">
                <h3 class="watchlist-section-header">
                    Serial Acquirers
                    <span class="watchlist-badge">watchlist/serial-acquirers</span>
                </h3>
                {watchlist_serial_acquirers_html}
            </div>
            
            <div style="height: 1px; background-color: #e2e8f0; margin: 24px 0;"></div>
            
            <div class="watchlist-section">
                <h3 class="watchlist-section-header">
                    Peptides
                    <span class="watchlist-badge">watchlist/peptides</span>
                </h3>
                {watchlist_peptides_html}
            </div>
            
            <div style="height: 1px; background-color: #e2e8f0; margin: 24px 0;"></div>
            
            <div class="watchlist-section">
                <h3 class="watchlist-section-header">
                    Generic
                    <span class="watchlist-badge">watchlist/generic</span>
                </h3>
                {watchlist_generic_html}
            </div>
        </div>

        <!-- SECTION 3: YOUTUBE STRATEGY INSIGHTS -->
        <div class="card inspiration-card">
            <div class="card-title-container">
                <span class="card-icon">📺</span>
                <h2 class="card-title">YouTube Strategy Insights</h2>
            </div>
            {youtube_strategists_html}
        </div>

        <!-- SECTION 4: AI PRODUCT INSPIRATION -->
        <div class="card inspiration-card">
            <div class="card-title-container">
                <span class="card-icon">💡</span>
                <h2 class="card-title">AI Product Inspiration</h2>
            </div>
            {ai_product_inspiration_html}
        </div>

        <div class="footer">
            <p>Sent with ❤️ from Kasona Intelligence Agent</p>
            <p>&copy; {datetime.now().year} Kasona. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""
    return html_report

def generate_weekly_digest(days: int, dry_run=False, local_mock=False, output_dir=None):
    sb = None
    grouped_chunks = {
        "markets/macro": [],
        "watchlist/serial-acquirers": [],
        "watchlist/peptides": [],
        "watchlist/Generic": [],
    }
    # These two are PURELY sender-based, never topic-based
    sender_buckets = {
        "ai_product_inspiration": [],
        "youtube_strategists": []
    }
    
    if local_mock:
        logger.info("=== RUNNING IN LOCAL MOCK MODE ===")
        mock_path = os.path.join(SKILL_DIR, "mock_data", "chunks.json")
        with open(mock_path, "r", encoding="utf-8") as f:
            all_chunks = json.load(f)
        logger.info(f"Loaded {len(all_chunks)} chunks from local mock.")
    else:
        sb = get_supabase_client()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        logger.info(f"Fetching chunks published since {cutoff}...")
        
        # Query all chunks, joining newsletter_inbox to get external_id and content_html
        res = sb.table("newsletter_chunks").select("content, metadata, source_type, published_at, primary_topic, newsletter_inbox(external_id, content_html)").gte("published_at", cutoff).execute()
        all_chunks = res.data or []
        logger.info(f"Found {len(all_chunks)} chunks in Supabase.")
        
    if not all_chunks:
        logger.warning("No chunks found in the given timeframe.")
        return
        
    PRODUCT_INSPIRATION = ["Dave Wang", "greymatter AI", "Lewis Jackson"]
    YOUTUBE_STRATEGISTS = ["FINANZFOKUS", "Moritz Hessel | Total Return Finanzen", "Moritz Hessel", "Total Return Finanzen", "Bravos Research", "Joseph Carlson", "Joseph Carlson Show", "Joseph Carlson After Hours"]

    # Split the chunks — sender-based routing is deterministic and takes priority
    for chunk in all_chunks:
        topic = chunk.get("primary_topic")
        sender = chunk.get("metadata", {}).get("sender", "")
        
        # DETERMINISTIC: Force these creators into their own sender-based bucket
        if sender in PRODUCT_INSPIRATION:
            sender_buckets["ai_product_inspiration"].append(chunk)
            continue
            
        if sender in YOUTUBE_STRATEGISTS:
            sender_buckets["youtube_strategists"].append(chunk)
            continue
            
        # Combine earnings, meta, and wealth-tech with macro for the market sentiment section
        if topic in ["markets/earnings", "investing/meta", "industry/wealth-tech"]:
            topic = "markets/macro"
            
        # For everything else, only append to topic-based groups (NOT sender_buckets)
        if topic in grouped_chunks:
            grouped_chunks[topic].append(chunk)
            
    # Merge sender_buckets into grouped_chunks for downstream processing
    grouped_chunks.update(sender_buckets)
            
    # Print statistics of categorized chunks
    logger.info("Categorized chunks:")
    for topic, chunks in grouped_chunks.items():
        logger.info(f"  - {topic}: {len(chunks)} chunks")
    
    if dry_run:
        logger.info("[DRY-RUN] Would generate HTML report from these sources:")
        for topic, chunks in grouped_chunks.items():
            sources = [c.get("metadata", {}).get("sender") for c in chunks]
            logger.info(f"  {topic} ({len(chunks)}): {sources}")
        return
        
    # Generate HTML report
    try:
        # Prepare directories for reports and source files
        reports_dir = output_dir or os.path.join(SKILL_DIR, "reports")
        sources_dir = os.path.join(reports_dir, "sources")
        os.makedirs(sources_dir, exist_ok=True)
        
        creator_meta = {}
        topic_sources = {}
        for topic, chunks in grouped_chunks.items():
            topic_sources[topic] = []
            for c in chunks:
                sender = c.get("metadata", {}).get("sender")
                if not sender: continue
                
                nl_inbox = c.get("newsletter_inbox")
                external_id = None
                content_html = None
                if isinstance(nl_inbox, dict):
                    external_id = nl_inbox.get("external_id")
                    content_html = nl_inbox.get("content_html") or None
                
                # Fallback for mock data
                if not external_id:
                    external_id = c.get("metadata", {}).get("external_id")
                
                source_type = c.get("source_type", "newsletter")
                
                # Clean up content_html if it was wrapped in quotes from the DB
                if content_html and content_html.startswith('"') and content_html.endswith('"'):
                    try:
                        content_html = json.loads(content_html)
                    except Exception:
                        content_html = content_html.strip('"')
                
                # Build source_url based on source_type:
                #   - youtube → YouTube watch URL
                #   - podcast → Notion page URL  
                #   - newsletter → create a local HTML file and link to it
                source_url = None
                if str(source_type).lower() == "youtube" and external_id:
                    source_url = f"https://www.youtube.com/watch?v={external_id}"
                elif str(source_type).lower() == "podcast" and external_id:
                    source_url = f"https://app.notion.com/p/julian-shares/9b991c68036742a9a7d80c853d367595?v=ba0fcd3b99fb4285869e4dad95a878e0&p={external_id}"
                elif str(source_type).lower() == "newsletter" and content_html and external_id:
                    source_filename = f"{external_id}.html"
                    source_filepath = os.path.join(sources_dir, source_filename)
                    with open(source_filepath, "w", encoding="utf-8") as sf:
                        sf.write(content_html)
                    source_url = f"sources/{source_filename}"

                # Track for Creator sections (AI inspiration / YouTube)
                if sender not in creator_meta:
                    creator_meta[sender] = {
                        "external_id": external_id,
                        "source_type": source_type,
                        "source_url": source_url,
                        "content_html": content_html
                    }
                
                # Track for Watchlist/Macro sections
                topic_sources[topic].append({
                    "sender": sender,
                    "external_id": external_id,
                    "source_type": source_type,
                    "source_url": source_url,
                    "content_html": content_html
                })
                    
        core_summaries = generate_core_digest_with_llm(grouped_chunks)
        creator_summaries = generate_creator_insights_with_llm(grouped_chunks)
        
        # Merge the two summary dictionaries
        summaries = {**core_summaries, **creator_summaries}
        
        summaries["_creator_meta"] = creator_meta # Pass it down to build_html_report safely
        summaries["_topic_sources"] = topic_sources # Pass it down for watchlist footers
        
        html_report = build_html_report(summaries)
        
        # Save to file
        filename = f"weekly-digest-{datetime.now().strftime('%Y-%m-%d')}.html"
        filepath = os.path.join(reports_dir, filename)
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_report)
            
        logger.info(f"✅ Success! HTML Report saved to: {filepath}")
        
    except Exception as e:
        logger.error(f"Error generating report: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Weekly Content Digest.")
    parser.add_argument("--days", type=int, default=7, help="Number of days to look back (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Print categorized sources without calling Gemini.")
    parser.add_argument("--local-mock", action="store_true", help="Run with local mock chunks instead of Supabase.")
    parser.add_argument("--output-dir", type=str, default=None, help="Directory to save reports (default: reports/ in skill dir)")
    
    args = parser.parse_args()
    generate_weekly_digest(days=args.days, dry_run=args.dry_run, local_mock=args.local_mock, output_dir=args.output_dir)

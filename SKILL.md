# 19_weekly-content-digest

This skill acts as a weekly digest aggregator. It pulls all newsletter and YouTube chunks added to the `newsletter_chunks` table over the last 7 days, categorizes them, and sends them to Gemini 2.5 Flash to generate a unified HTML report.

## How it Works

1. **Query**: Connects to the Market Data Supabase and queries `newsletter_chunks` for records where `published_at` is within the last N days.
2. **Categorize**: Uses a hardcoded `PRODUCT_INSPIRATION` list (Dave Wang, greymatter AI, Lewis Jackson) to split the content into two buckets:
   - **Market Data**: Standard copytraders and analysts.
   - **Product Inspiration**: Creators focused on AI features and product ideas.
3. **Generate**: Passes both buckets into a single LLM prompt, asking Gemini to synthesize market sentiment, hot sectors, and product ideas into a stylized HTML email report.
4. **Save**: Outputs the HTML report to the local `/reports/` folder.

## Setup

Ensure your `.env` file (located in `invest_analysis-main/research-meeting-skill/config/.env` or the project root) contains:

```bash
SUPABASE_URL=https://<your-project>.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key
GEMINI_API_KEY=your-gemini-api-key
```

Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

**Generate a report from live Supabase data (last 7 days):**
```bash
python scripts/generate_weekly_digest.py
```

**Generate a report looking back 14 days:**
```bash
python scripts/generate_weekly_digest.py --days 14
```

**Test locally without hitting Supabase (uses mock_data/chunks.json):**
```bash
python scripts/generate_weekly_digest.py --local-mock
```

**Dry-run to see which sources will be categorized where (no LLM usage):**
```bash
python scripts/generate_weekly_digest.py --dry-run
```

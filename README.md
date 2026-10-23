# Daily Briefing

Every evening at 6 PM IST you get one email with: a 60-second summary, 5 top stories per
category (India, Politics, World, Protests, AI, Environment, Business) each with a short
summary and a link, plus a "Watch" section with video links and thumbnails.

## How it works
RSS feeds + YouTube channel feeds -> filter last 26h and dedupe -> ONE call to Google's free Gemini
API picks the important stories and summarizes -> HTML email via Gmail SMTP -> GitHub Actions
runs it on a schedule. Total cost: Rs 0.

## Setup (about 10 minutes)
1. Create a **private** GitHub repo and push this folder to it.
2. Get a free Gemini API key: open aistudio.google.com, sign in, click "Get API key". No billing or card needed.
3. Gmail: turn on 2-Step Verification, then create an **App Password**
   (Google Account -> Security -> App passwords). Do not use your normal password.
4. Repo -> Settings -> Secrets and variables -> Actions -> add:
   - GEMINI_API_KEY
   - SMTP_USER   (your Gmail address)
   - SMTP_PASS   (the 16-character app password)
   - EMAIL_TO    (where to deliver; can be the same address)
5. Actions tab -> "Daily Briefing" -> **Run workflow** to test. After that it runs daily at 6 PM IST.

## Test locally
    pip install -r requirements.txt
    export GEMINI_API_KEY=...
    python briefing.py --dry-run     # writes preview.html, sends nothing

## Customize
Everything is in config.yaml: add feeds, add categories, change story counts, change YouTube channels.
Change the time by editing the cron line in .github/workflows/daily.yml (cron is in UTC).

## Notes
- GitHub's scheduler can start a few minutes late, so "6 PM" may arrive 6:05-6:20.
- Some sites change or remove RSS URLs. The script skips broken feeds and logs a warning, so check
  the Actions log occasionally.
- Free-tier caveats: Google changes free limits and model names often. The script makes only 1 request
  per day and uses the `gemini-flash-latest` alias, so it should stay within limits. If it ever fails,
  it still sends the email with raw feed snippets instead of AI summaries. You can set a GEMINI_MODEL
  secret/env var to pin a specific model. On the free tier Google may use your prompts to improve its
  products, which is fine here because it only sees public news headlines.
- GitHub Actions is free for public repos, and private repos get a monthly free-minutes allowance far
  above the ~1 minute per day this uses. Gmail SMTP is free.
- Optional: set ANTHROPIC_API_KEY instead of GEMINI_API_KEY to use Claude (paid) and CLAUDE_MODEL to choose the model.

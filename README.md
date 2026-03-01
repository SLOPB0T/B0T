# B0T v.1

An automated X moderation bot that uses AI to detect and report KOL Slop/Sponsored content.

---

## How It Works

```
                           COMMENT ARRIVES
                                  |
                                  v
+----------------------------------------------------------------+
| LAYER 1: MUST-ESCALATE PATTERNS                                |
|                                                                |
|  Check for: slurs, self-harm, threats, violence                |
|  These ALWAYS send to LLM (no ML override allowed)             |
|                                                                |
|  Also check: dismissive language, direct insults, shill accuse |
|  These are "soft" patterns - ML consensus can override them    |
|                                                                |
|  If triggered -> Get ML scores -> Check for ML override...     |
+----------------------------------------------------------------+
                                  |
                   +--------------+--------------+
                   |                             |
            Soft pattern                   Hard pattern
            (insult, shill, etc.)          (slur, threat, etc.)
                   |                             |
                   v                             |
+----------------------------------+             |
| ML CONSENSUS OVERRIDE            |             |
|                                  |             |
| If ALL 3 models score < 0.30:    |             |
|   -> SKIP (ML agrees benign)     |             |
| Else:                            |             |
|   -> SEND TO LLM                 |-------------+
+----------------------------------+             |
                                                 |
                                                 v
                                          SEND TO LLM
                                                 |
                            (Pattern didn't trigger)
                                                 |
                                                 v
+----------------------------------------------------------------+
| LAYER 2: RUN ALL ML MODELS                                     |
|                                                                |
|  - Detoxify (local) - triggers on profanity/edgy content       |
|  - OpenAI Moderation API - better context understanding        |
|  - Google Perspective API - better context understanding       |
|                                                                |
|  Record which models triggered (exceeded their thresholds)     |
+----------------------------------------------------------------+
                                  |
                                  v
+----------------------------------------------------------------+
| LAYER 3: DECISION LOGIC                                        |
|                                                                |
|  Case A: ONLY Detoxify triggered (OpenAI & Persp did NOT)      |
|    -> If external_max < 0.30: SKIP (not validated)             |
|    -> If external_max >= 0.30: SEND (external validates)       |
|                                                                |
|  Case B: OpenAI OR Perspective triggered                       |
|    -> SEND TO LLM                                              |
|                                                                |
|  Case C: Nothing triggered                                     |
|    -> SKIP                                                     |
+----------------------------------------------------------------+
                                  |
                              If SEND
                                  |
                                  v
+----------------------------------------------------------------+
| LAYER 4: LLM REVIEW                                            |
|                                                                |
|  Send to Groq/Grok LLM with full context:                      |
|  - Moderation guidelines                                       |
|  - Post title, parent comment, grandparent context             |
|  - All ML scores from Detoxify, OpenAI, Perspective            |
|  - Pattern match trigger reasons                               |
|                                                                |
|  LLM returns: VERDICT: REPORT or VERDICT: BENIGN               |
+----------------------------------------------------------------+
                                  |
                   +--------------+--------------+
                   |                             |
                BENIGN                        REPORT
                   |                             |
                   v                             v
            No action                   Report to Reddit
                                                 |
                                          (after 24h)
                                                 v
                                      +-------------------+
                                      |  ACCURACY CHECK   |
                                      |  Removed = TP     |
                                      |  Still up = FP    |
                                      +-------------------+
```

### ML Consensus Override

When a "soft" pattern triggers (insult, dismissive, shill accusation, etc.), the bot checks if all three ML models agree the content is benign (scores < 0.30). If so, it skips the expensive LLM call.

**Soft patterns** (ML override allowed): `insult`, `dismissive_soft`, `dismissive_hard`, `shill_accusation`, `regex_pattern`, `brigading`

**Hard patterns** (always go to LLM): `slur`, `self-harm`, `threat`, `sexual_violence`, `violence_illegal`

This saves LLM costs on false pattern matches like:
- "balls of light" triggering "balls" insult pattern
- "Dumb question, but..." triggering "dumb" pattern
- "knee-jerk reaction" triggering "jerk" pattern

### Decision Examples

| Comment | Layer 1 | Detox | OpenAI | Persp | Result |
|---------|---------|-------|--------|-------|--------|
| "I think UFOs are real" | - | 0.00 | 0.01 | 0.02 | **SKIP** (nothing triggered) |
| "it's a fucking plane" | - | 0.95 | 0.10 | 0.15 | **SKIP** (detox-only, external < 0.30) |
| "Holy shit that's cool" | - | 0.90 | 0.05 | 0.10 | **SKIP** (detox-only, external < 0.30) |
| "Same old bullshit argument" | benign | - | - | - | **SKIP** (Layer 1 benign match) |
| "Dumb question, but..." | insult | 0.16 | 0.01 | 0.03 | **SKIP** (ML consensus < 0.30) |
| "three balls of light" | insult | 0.22 | 0.00 | 0.04 | **SKIP** (ML consensus < 0.30) |
| "Edgy comment" | - | 0.80 | 0.10 | 0.35 | **SEND** (detox + external >= 0.30) |
| "You're an idiot" | insult | 0.85 | 0.75 | 0.40 | **SEND** (ML scores high, no override) |
| "Hate speech" | - | 0.91 | 0.89 | 0.72 | **SEND** (multiple triggered) |
| "Kill yourself" | must_escalate | - | - | - | **SEND** (Layer 1 self-harm, hard pattern) |
| "You're a retard" | must_escalate | - | - | - | **SEND** (Layer 1 slur, hard pattern) |

### Key Insight: Why External Validation?

Detoxify triggers on **any profanity** regardless of context. "It's a fucking plane" scores 0.95+ toxicity even though it's not attacking anyone.

OpenAI and Perspective are **better at understanding context**. When Detoxify triggers alone but external APIs score low (< 0.30), it's usually a false positive.

**The rule:** Detox-only triggers require external validation (score >= 0.30) to send to LLM.

---

## External Moderation APIs

The bot uses three ML models for toxicity detection. Detoxify runs locally (free, unlimited). OpenAI and Perspective are external APIs that provide better context understanding.

### Google Perspective API (Recommended - Free)

**Setup:**
1. Go to https://developers.perspectiveapi.com/s/docs-get-started
2. Click "Get Started" and request API access
3. Access is typically granted within 1-2 business days
4. Once approved, create an API key in your Google Cloud Console

**Cost:** Free (quota-based, very generous limits)

```bash
PERSPECTIVE_API_KEY=your_key_here
PERSPECTIVE_ENABLED=true
PERSPECTIVE_MODE=all
PERSPECTIVE_THRESHOLD=0.70
PERSPECTIVE_RPM=60
```

Note: Perspective only supports certain languages (English, Spanish, French, German, etc.). Comments in unsupported languages are silently skipped.

### OpenAI Moderation API (Recommended - Free*)

**Setup:**
1. Create an account at https://platform.openai.com
2. Add a minimum of $5 credit to your account
3. Generate an API key

**Cost:** The Moderation API itself is **free** and doesn't consume credits. However, OpenAI now requires an account with credits to use any API endpoints, including free ones. The $5 minimum deposit is never used by the moderation endpoint - it just needs to be there.

```bash
OPENAI_API_KEY=sk-xxxxx
OPENAI_MODERATION_ENABLED=true
OPENAI_MODERATION_MODE=all
OPENAI_MODERATION_THRESHOLD=0.50
OPENAI_MODERATION_RPM=10
```

### API Mode Options

Both external APIs support three modes:

| Mode | Behavior | API Calls |
|------|----------|-----------|
| `all` | Run on every comment | High (recommended) |
| `confirm` | Only run if Detoxify triggers | Medium |
| `only` | Skip Detoxify, use only this API | Medium |

**Recommended:** Use `MODE=all` for both APIs. This ensures external validation is always available for the detox-only skip logic.

### Detoxify (Local - Free, Unlimited)

Detoxify runs locally using a pre-trained ML model. No API key needed.

**Thresholds** (configurable in `.env`):

| Label | Directed at User | Not Directed |
|-------|------------------|--------------|
| threat | 0.15 | 0.15 |
| severe_toxicity | 0.20 | 0.20 |
| identity_attack | 0.25 | 0.25 |
| insult | 0.40 | 0.65 |
| toxicity | 0.50 | 0.65 |
| obscene | 0.90 | 0.90 |

"Directed" = contains "you", "your", "OP", or is a reply (excluding "generic you" phrases like "you don't need to", "if you think about it").

**Detoxify Escalation Control:**

If Detoxify triggers too many false positives, you can disable it from triggering LLM review while still using it for scoring context:

```bash
# Detoxify provides scores but won't trigger LLM review on its own
DETOXIFY_CAN_ESCALATE=false

# Let OpenAI and Perspective decide what gets sent to LLM
OPENAI_MODERATION_MODE=all
PERSPECTIVE_MODE=all
```

---

## What Gets Reported vs Ignored

### Gets Reported
- Direct insults at other users ("you're an idiot", "what a moron")
- Slurs and hate speech (including obfuscated: "n1gger", "f4g")
- Threats ("I'll find you", "you're dead")
- Self-harm encouragement ("kill yourself", "kys")
- Shill/bot accusations at users ("you're a fed", "obvious bot")
- Calls for violence ("someone should shoot that", "laser the plane")

### Does NOT Get Reported
- Criticizing ideas ("that theory is nonsense", "this has been debunked")
- Criticizing public figures ("Corbell is a grifter", "Greer is a fraud")
- Profanity for emphasis ("holy shit that's crazy", "what the fuck")
- Skepticism ("this is obviously fake", "that's just Starlink")
- Self-deprecation ("I'm such an idiot", "maybe I'm just dumb")
- Third-party criticism ("the idiots who run the government")
- Situation criticism ("this is so stupid", "what a dumb rule")
- Venting about the subreddit ("this sub sucks", "mods are useless")
- Disagreement ("you're wrong", "I completely disagree")

---

## Understanding moderation_patterns.json

This file contains word/phrase lists that the bot uses for fast pre-filtering BEFORE calling the AI. It's organized into categories:

### Slurs (Always escalate to AI)

```json
"slurs": {
  "racial": ["n-word variants", "..."],
  "homophobic_hard": ["f-word variants", "..."],
  "transphobic_hard": ["tranny", "..."],
  "ableist_hard": ["retard", "..."]
}
```

These are high-confidence bad words that almost always indicate a problem. Any match immediately sends the comment to the AI for review.

### Contextual Sensitive Terms (Escalate with additional signals)

```json
"contextual_sensitive_terms": {
  "racial_ambiguous": ["negro", "cracker", "gringo", "..."],
  "sexual_orientation": ["homo", "queer", "..."],
  "ideology_terms": ["white power", "nazi", "..."]
}
```

These words CAN be used in neutral contexts (historical discussion, quoting, reclaimed terms). They only escalate if:
- Directed at a user ("you're just a [term]"), OR
- Combined with high identity_attack score from Detoxify

### Insults (Escalate when directed at users)

```json
"insults_direct": {
  "intelligence": ["idiot", "moron", "dumbass", "..."],
  "character": ["loser", "pathetic", "scumbag", "..."],
  "mental_health": ["take your meds", "you're crazy", "..."]
}
```

These escalate when the comment appears to be targeting another user (contains "you", "your", "OP", or is a reply).

### Threats & Self-Harm (Always escalate)

```json
"threats": {
  "direct": ["I'll kill you", "you're dead", "..."],
  "implied": ["watch your back", "I know where you live", "..."]
},
"self_harm": {
  "direct": ["kill yourself", "kys", "..."],
  "indirect": ["world would be better without you", "..."]
}
```

High-priority patterns that always go to the AI.

### Benign Skip Phrases (~950 patterns)

```json
"benign_skip": {
  "frustration_exclamations": ["holy shit", "what the fuck", "this shit", "..."],
  "profanity_as_emphasis": ["fucking ridiculous", "so fucking", "it's a fucking", "..."],
  "slang_expressions": ["full of shit", "talk shit", "copium", "..."],
  "self_deprecating": ["I'm just dumb", "maybe I'm stupid", "..."],
  "third_party_profanity": ["idiots who run", "these morons", "..."],
  "disbelief_at_situation": ["this is bullshit", "total crap", "so stupid", "..."],
  "ufo_context_phrases": ["crazy footage", "insane video", "..."],
  "ufo_skepticism_phrases": ["this is fake", "obviously a drone", "clearly CGI", "..."],
  "genuine_questions": ["what exactly is a", "how is he a", "..."]
}
```

When a comment matches these AND isn't directed at a user, the benign pattern helps prevent false positives from pattern matching and provides context to the ML decision logic.

### Generic "You" Detection

The bot distinguishes between personal attacks and generic statements:
- **"You're an idiot"** -> Personal attack = escalate
- **"You can't just ignore this"** -> Generic = don't escalate

Currently has **170+ generic "you" phrases** including:
- Hypotheticals: "if you think", "when you look at"
- Generic advice: "you don't need", "you can't expect"
- Rhetorical: "wouldn't you", "don't you think"
- All with apostrophe variants: "dont", "cant", "wouldnt"

### How Pattern Matching Works

1. **Word boundary matching** - "cope" won't match "telescope", "pos" won't match "possessive"
2. **Normalization** - "n1gg3r" gets normalized to check against patterns
3. **Directedness check** - Many patterns only escalate when aimed at a user
4. **Context awareness** - Top-level comments vs replies are treated differently
5. **Benign pattern validation** - Dismissive/insult patterns check for benign phrases first

---

## LLM Configuration

### Model Fallback Chain

The bot uses a primary model with fallbacks for rate limiting:

```bash
# Best setup: Paid Grok primary + Free Groq reasoning fallbacks
LLM_MODEL=grok-4-0709
LLM_FALLBACK_CHAIN=openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3-32b
GROQ_REASONING_EFFORT=high
```

### Recommended Models

**Reasoning Models (Recommended for nuanced moderation):**

| Model | Provider | Reasoning | Cost/Limits | Notes |
|-------|----------|-----------|-------------|-------|
| `grok-4-0709` | x.ai (paid) | Always | ~$2-5/M in | Best quality, always reasons |
| `openai/gpt-oss-120b` | Groq (free) | Configurable | 1K RPD, 200K TPD | Best free reasoning |
| `openai/gpt-oss-20b` | Groq (free) | Configurable | 1K RPD, 200K TPD | Smaller, still reasons |
| `qwen/qwen3-32b` | Groq (free) | Always | 1K RPD, 500K TPD | Good limits |

**Non-Reasoning Models (faster but less accurate on gray areas):**

| Model | Provider | Limits | Notes |
|-------|----------|--------|-------|
| `groq/compound` | Groq (free) | 250 RPD, unlimited TPD | Smart routing, no reasoning |
| `llama-3.3-70b-versatile` | Groq (free) | 1K RPD, 100K TPD | Good quality |
| `llama-3.1-8b-instant` | Groq (free) | 14.4K RPD, 500K TPD | Fast fallback |

**Reasoning Effort Settings:**
```bash
# For Groq's gpt-oss models (low/medium/high)
GROQ_REASONING_EFFORT=high

# For x.ai's grok-3-mini only (grok-4 always reasons)
#XAI_REASONING_EFFORT=high
```

**Smart Cooldown System**: When rate limited, the bot remembers which models are unavailable and skips them automatically. Cooldowns include a 60-second buffer to ensure rate limits fully reset.

### LLM Context

The LLM receives comprehensive context for each decision:
- Your moderation guidelines
- Whether comment is `[TOP-LEVEL]` or `[REPLY]`
- Post title
- Parent comment text and author (with OP indicator)
- Grandparent comment text and author
- All ML scores from Detoxify, OpenAI, Perspective
- Pattern match trigger reasons

---

## Auto-Remove (Optional)

For high-confidence toxic comments, auto-remove to mod queue:

```bash
AUTO_REMOVE_ENABLED=true

# Which models must agree (comma-separated): detoxify, openai, perspective
AUTO_REMOVE_REQUIRE_MODELS=openai,perspective

# How many must pass their threshold (2 = both must agree)
AUTO_REMOVE_MIN_CONSENSUS=2

# Minimum scores required
AUTO_REMOVE_OPENAI_MIN=0.80
AUTO_REMOVE_PERSPECTIVE_MIN=0.80

# Auto-remove on pattern matches (slurs, threats)?
AUTO_REMOVE_ON_PATTERN_MATCH=false
```

| Scenario | Action |
|----------|--------|
| LLM=REPORT, OpenAI=0.85, Perspective=0.90 | **AUTO-REMOVE** |
| LLM=REPORT, OpenAI=0.60, Perspective=0.90 | Report only (OpenAI too low) |
| LLM=REPORT, OpenAI disabled | Report only (can't reach consensus) |
| LLM=BENIGN | No action (LLM has final say) |

Auto-removed comments:
- Go to mod queue immediately
- Stay there until a mod approves or confirms removal
- Get a special Discord notification (purple) with all ML scores

---

## Features

- **Smart pre-filtering** - Only ~5% of comments use your API quota
- **~950 benign phrases** - Automatically handles common expressions, slang, and profanity-as-emphasis
- **170+ generic "you" phrases** - Distinguishes "you're an idiot" from "you don't need to be an expert"
- **External validation** - Detox-only triggers require OpenAI/Perspective validation (>= 0.30)
- **Misspelling/variant detection** - Catches "stoopid", "ur", "dont", leetspeak like "n1gg3r"
- **Quote detection** - Understands Reddit quote blocks (lines starting with ">")
- **Context-aware** - Knows if it's a reply vs top-level, who's being targeted
- **Public figure detection** - Understands UFO community figures (Grusch, Elizondo, Corbell, etc.)
- **Multi-model consensus** - Combines Detoxify, OpenAI, and Perspective for better accuracy
- **ML scores sent to LLM** - AI sees detector scores and thresholds for informed decisions
- **Auto-remove option** - Automatically remove high-confidence toxic comments (configurable consensus)
- **Detoxify escalation control** - Optionally disable Detoxify from triggering LLM review
- **Model fallback chain** - Automatically switches models if rate limited
- **Discord notifications** - Real-time alerts with trigger reasons
- **Accuracy tracking** - Logs false positives for tuning
- **Dry run mode** - Test without actually reporting

---

## Requirements

- Python 3.9+
- Reddit account with mod permissions (for moderator reports)
- Groq API key (free at https://console.groq.com)
- x.ai API key (optional, paid - for Grok models at https://console.x.ai)
- OpenAI API key (optional, requires $5 deposit but moderation is free)
- Google Perspective API key (optional, free at https://developers.perspectiveapi.com)
- Discord webhook (optional, for notifications)

---

## Quick Start

### 1. Clone and install dependencies

```bash
git clone https://github.com/YOUR_USERNAME/toxic-report-bot.git
cd toxic-report-bot
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp env.template .env
# Edit .env with your credentials
```

### 3. Add your moderation guidelines

Edit `moderation_guidelines.txt` to customize what the AI should report.

### 4. Test in dry run mode

```bash
# Make sure DRY_RUN=true in .env
python bot.py
```

### 5. Go live

```bash
# Set DRY_RUN=false in .env
python bot.py
```

---

## Configuration

All configuration is done via environment variables in a `.env` file. 

### Setting Up Your .env File

1. Copy the template: `cp env.template .env`
2. Edit `.env` with your credentials
3. **Never commit `.env` to git** - it contains secrets!

The `env.template` file has detailed comments explaining each option.

### Reddit Credentials

You need to create a Reddit "script" app to get these:

1. Go to https://www.reddit.com/prefs/apps
2. Click "create another app..."
3. Select "script"
4. Fill in name and redirect URI (use `http://localhost:8080`)
5. Copy the client ID (under the app name) and secret

| Variable | Description | Example |
|----------|-------------|---------|
| `REDDIT_CLIENT_ID` | OAuth app client ID | `Ab3CdEfGhIjKlM` |
| `REDDIT_CLIENT_SECRET` | OAuth app client secret | `xYz123AbC456DeF789` |
| `REDDIT_USERNAME` | Bot account username | `ToxicReportBot` |
| `REDDIT_PASSWORD` | Bot account password | `your_secure_password` |
| `REDDIT_USER_AGENT` | Identifies your bot to Reddit | `toxic-report-bot/2.0 by u/YourUsername` |
| `SUBREDDITS` | Comma-separated list | `UFOs` or `UFOs,aliens,UAP` |

### LLM Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | (required) | Your Groq API key (starts with `gsk_`) |
| `LLM_MODEL` | `grok-4-0709` | Primary model to try first |
| `LLM_FALLBACK_CHAIN` | (see below) | Comma-separated list of fallback models |
| `LLM_REQUESTS_PER_MINUTE` | `2` | Rate limit (1 request per 30 sec) |

### Detection Thresholds

Thresholds control how sensitive the pre-filter is. Scores range from 0.0 to 1.0.

| Variable | Default | What it catches |
|----------|---------|-----------------|
| `THRESHOLD_THREAT` | `0.15` | Threats of violence/harm |
| `THRESHOLD_SEVERE_TOXICITY` | `0.20` | Extreme toxic content |
| `THRESHOLD_IDENTITY_ATTACK` | `0.25` | Slurs, hate speech |
| `THRESHOLD_INSULT_DIRECTED` | `0.40` | Insults aimed at users |
| `THRESHOLD_INSULT_NOT_DIRECTED` | `0.65` | General insults not at a user |
| `THRESHOLD_TOXICITY_DIRECTED` | `0.50` | Toxic comments at users |
| `THRESHOLD_TOXICITY_NOT_DIRECTED` | `0.65` | General toxic comments |
| `THRESHOLD_OBSCENE` | `0.90` | Profanity (keep high) |

### Reporting

| Variable | Default | Description |
|----------|---------|-------------|
| `REPORT_AS` | `moderator` | `moderator` (needs mod perms) or `user` |
| `ENABLE_REDDIT_REPORTS` | `true` | Master switch for reporting |
| `DRY_RUN` | `false` | `true` = log only, don't actually report |

**Important:** Start with `DRY_RUN=true` to test before going live!

---

## Discord Notifications

The bot supports two Discord integration methods:

### Option 1: Webhook (Simple)

Basic notifications via Discord webhook. Messages are static (don't update).

**Setup:**
1. Go to your Discord server
2. Edit a channel -> Integrations -> Webhooks -> New Webhook
3. Copy the webhook URL to your `.env`

```bash
ENABLE_DISCORD=true
DISCORD_WEBHOOK=https://discord.com/api/webhooks/xxx/yyy
```

### Option 2: Discord Bot (Advanced)

More advanced integration using a Discord bot account. Messages are **editable** and update in-place when mod actions occur.

**Benefits:**
- Messages update in real-time (shows removed/approved status)
- Cleaner review queue (no duplicate notifications)
- Track pending reviews directly in Discord

**Setup:**
1. Go to https://discord.com/developers/applications
2. Create a new application -> Bot -> Reset Token -> Copy token
3. Enable **MESSAGE CONTENT INTENT** under Bot -> Privileged Gateway Intents
4. Generate invite URL: OAuth2 -> URL Generator -> Select `bot` scope
5. Add permissions: Send Messages, Read Message History, Embed Links
6. Invite bot to your server using the generated URL
7. Enable Developer Mode in Discord (User Settings -> Advanced)
8. Right-click your review channel -> Copy ID
9. Add to your `.env`:

```bash
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_REVIEW_CHANNEL_ID=1234567890123456789
DISCORD_REVIEW_CHECK_INTERVAL=120
```

The bot will post editable messages that show:
- Initial report with all ML scores and LLM reasoning
- Updated status when mods remove or approve the comment
- Color changes: Red (reported) -> Green (removed) or Orange (approved/FP)

### Notification Types

| Notification | Color | Meaning |
|--------------|-------|---------|
| Bot Started | Green | Bot is running |
| Moderation Stats | Varies | Stats summary (on startup and daily at midnight UTC) |
| Borderline Skip | Gray | Scored kinda high but not reviewed |
| Analyzing | Blue | Sending to AI for review |
| BENIGN | Green | AI says it's fine |
| REPORT | Red | AI flagged it, reporting |
| AUTO-REMOVE | Purple | Auto-removed to mod queue |
| False Positive | Orange | Reported comment wasn't removed |

---

## Accuracy Tracking & Stats

The bot tracks every report and provides comprehensive statistics.

### Stats Overview

1. **Bot Pipeline Stats** (`bot_stats.json`) - What the bot processed:
   - Scanned: Total comments analyzed
   - Benign-skipped: Comments skipped by benign phrase detection
   - Sent to LLM: Comments sent to AI for review

2. **Outcome Stats** (`pending_reports.json`) - What happened to reported comments:
   - Removed: Mods removed the comment (true positive)
   - Approved: Mods cleared the report (false positive)
   - Pending: Awaiting mod action

### Discord Stats Display

On startup and daily at midnight UTC, the bot posts a stats summary with confirm rates for 24h, 7d, and all-time.

**Confirm Rate** = % of bot escalations that mods removed (precision metric).

### Accuracy Targets

| Confirm Rate | Assessment | Action |
|--------------|------------|--------|
| 80%+ | Excellent | Bot is well-tuned |
| 60-80% | Good | Review false positives occasionally |
| 40-60% | Needs work | Review guidelines and thresholds |
| Below 40% | Too aggressive | Raise thresholds significantly |

---

## Deploying on Ubuntu Server

The bot runs great on free cloud instances. It uses minimal resources (~200MB RAM) and can run 24/7 for free.

### Free Cloud Options

| Provider | Free Tier | Specs | Link |
|----------|-----------|-------|------|
| **Oracle Cloud** | Forever free | 1 CPU, 1GB RAM, 50GB disk | [cloud.oracle.com](https://cloud.oracle.com) |
| **Google Cloud** | Free e2-micro | 0.25 CPU, 1GB RAM | [cloud.google.com](https://cloud.google.com) |
| **AWS** | 12 months free | t2.micro, 1GB RAM | [aws.amazon.com](https://aws.amazon.com) |

Oracle Cloud's "Always Free" tier is recommended - it never expires and has plenty of resources.

### Step-by-Step Ubuntu Setup

#### 1. Create your cloud instance

- Choose **Ubuntu 22.04 or 24.04 LTS** (minimal/server image)
- Open port 22 (SSH) in your security rules
- Save your SSH key

#### 2. Connect via SSH

```bash
ssh ubuntu@YOUR_SERVER_IP
```

#### 3. Install system dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and pip
sudo apt install -y python3 python3-pip python3-venv git
```

#### 4. Clone and setup the bot

```bash
# Clone the repo
git clone https://github.com/slopb0t/b0t.git
cd b0t

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies (this takes a few minutes on micro instances)
pip install --upgrade pip
pip install -r requirements.txt
```

#### 5. Configure the bot

```bash
# Create your config file
cp env.template .env

# Edit with your credentials
nano .env
```

Fill in your Reddit credentials, Groq API key, and Discord webhook (optional).

#### 6. Create moderation guidelines

```bash
# Copy the template
cp moderation_guidelines_template.txt moderation_guidelines.txt

# Customize for your subreddit
nano moderation_guidelines.txt
```

#### 7. Test the bot

```bash
# Make sure DRY_RUN=true in .env first!
source .venv/bin/activate
python bot.py
```

Watch the logs. If it connects and starts scanning comments, you're good.

#### 8. Set up as a system service

Create the service file:

```bash
sudo nano /etc/systemd/system/b0t.service
```

Paste this:

```ini
[Unit]
Description=xSpamReportBot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/xSpamb0t
Environment=PATH=/home/ubuntu/xSpamb0t/.venv/bin
ExecStart=/home/ubuntu/xSpamb0t/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable toxicreportbot
sudo systemctl start toxicreportbot
```

#### 9. Go live

Once testing looks good:

```bash
# Edit .env and set DRY_RUN=false
nano .env

# Restart the service
sudo systemctl restart toxicreportbot
```

### Useful Commands

```bash
# View live logs
sudo journalctl -u toxicreportbot -f

# Check status
sudo systemctl status toxicreportbot

# Restart after config changes
sudo systemctl restart toxicreportbot

# Stop the bot
sudo systemctl stop toxicreportbot

# View recent logs
sudo journalctl -u toxicreportbot --since "1 hour ago"
```

### Understanding Log Output

The bot logs show ML scores from all APIs for each comment:

```
# Normal comment - all scores low, skipped
PREFILTER | SKIP | Detox:0.02 | OpenAI:0.01 | Persp:0.03 | 'I think UFOs are real...'

# Detox triggered but external low - skipped
PREFILTER | SKIP (detox-only, external APIs low: OpenAI=0.10, Persp=0.15) | Detox:0.95 | OpenAI:0.10 | Persp:0.15 | 'it's a fucking plane...'

# Soft pattern triggered but ML consensus says benign - skipped (saves LLM cost)
PREFILTER | ML_CONSENSUS_SKIP | Pattern 'must_escalate:insult+reply' but ML scores all <0.3 (detox=0.16, openai=0.00, persp=0.04) | 'Dumb question but...'

# Sent to LLM - external APIs triggered
PREFILTER | SEND (detoxify:toxicity=0.91 + openai:harassment=0.89) [directed, reply] | Detox:0.91 | OpenAI:0.89 | Persp:0.72 | 'you're pathetic...'

# Must-escalate hard pattern (slur, threat, etc.) - always goes to LLM
PREFILTER | MUST_ESCALATE (must_escalate:slur) | 'you retard...'

# Must-escalate soft pattern with high ML scores - goes to LLM
PREFILTER | MUST_ESCALATE (must_escalate:insult+directed) | 'you're such an idiot...'
```

Score meanings:
- **Detox**: Detoxify toxicity score (0-1)
- **OpenAI**: Max of harassment/hate from OpenAI Moderation (0-1)
- **Persp**: Max of TOXICITY/INSULT from Google Perspective (0-1)

### Updating the Bot

```bash
cd ~/XToxicReportBot
git pull
sudo systemctl restart toxicreportbot
```

### Memory Considerations

On 1GB RAM instances, the first startup takes ~60 seconds while Detoxify loads its ML model. After that, it uses ~200-300MB steadily. If you run into memory issues:

```bash
# Add swap space (one-time setup)
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## Customization

### Moderation Guidelines

The `moderation_guidelines.txt` file is the "brain" of the bot - it tells the AI exactly what to report and what to ignore. This is where you customize behavior for your subreddit.

**Use `moderation_guidelines_template.txt` as a starting point** - it has detailed comments explaining each section.

Key sections to customize:

| Section | What to Change |
|---------|----------------|
| Subreddit name | Replace `r/YOUR_SUBREDDIT_HERE` |
| Public figures list | Add people commonly discussed in your community |
| Shill accusations | Add domain-specific accusations (e.g., "paid shill for [company]") |
| Dangerous acts | Add things specific to your topic (e.g., "laser aircraft" for UFOs) |
| Benign phrases | Add skepticism phrases common in your community |

**Example customizations by subreddit type:**

For **r/politics**:
```
PUBLIC FIGURES: Biden, Trump, AOC, Pelosi, McConnell, etc.
BENIGN: "both sides", "whataboutism", "fake news"
```

For **r/nba**:
```
PUBLIC FIGURES: LeBron, Curry, team owners, coaches
BENIGN: "refs are blind", "trade him", "bust"
```

For **r/cryptocurrency**:
```
PUBLIC FIGURES: CZ, SBF, Vitalik, crypto influencers
SHILL ACCUSATIONS: "paid by [coin]", "bag holder"
BENIGN: "FUD", "shill coin", "rug pull"
```

### Pattern Lists

Edit `moderation_patterns.json` to add/remove:
- Slurs and hate speech terms
- Insult words and phrases
- Threat phrases
- Benign skip phrases
- Public figure names

---

## Files

| File | Description |
|------|-------------|
| `bot.py` | Main bot code |
| `moderation_guidelines.txt` | Instructions for the AI on what to report (customize this!) |
| `moderation_guidelines_template.txt` | Annotated template with explanations for customization |
| `moderation_patterns.json` | Word lists for pre-filtering (~1700+ patterns: benign skips, slurs, insults, etc.) |
| `env.template` | Template for `.env` configuration |
| `requirements.txt` | Python dependencies |
| `bot_stats.json` | Auto-generated bot pipeline stats (persists across restarts) |
| `pending_reports.json` | Auto-generated tracking of reported comments and outcomes |
| `false_positives.json` | Auto-generated log of false positives (reported but not removed) |
| `benign_analyzed.json` | Auto-generated log of comments sent to LLM that were benign |

---

## Troubleshooting

### Bot not reporting anything
- Check `DRY_RUN` is `false`
- Check `ENABLE_REDDIT_REPORTS` is `true`
- Check bot has mod permissions in the subreddit

### Rate limited constantly
- Check Groq dashboard for usage: https://console.groq.com/settings/organization/usage
- The fallback chain should handle this automatically
- If ALL models are exhausted, add more models to `LLM_FALLBACK_CHAIN`
- Consider adding x.ai Grok models as fallback (requires `XAI_API_KEY`)
- Lower `LLM_REQUESTS_PER_MINUTE` to `1` for slower but safer operation
- Look for "Skipping [model] - on cooldown" in logs to see what's happening

### High false positive rate
- Review `false_positives.json` to see patterns
- Update `moderation_guidelines.txt` with more examples
- Add common benign phrases to `benign_skip` in `moderation_patterns.json`
- Raise thresholds in `.env` (e.g., `THRESHOLD_INSULT_DIRECTED=0.50`)
- Enable both OpenAI and Perspective with `MODE=all` for better external validation

### Discord notifications not working

**Webhook issues:**
- Check webhook URL is correct
- Check for "Discord embed post failed" in logs
- Test webhook with curl:
  ```bash
  curl -X POST -H "Content-Type: application/json" \
    -d '{"content":"Test message"}' \
    "YOUR_WEBHOOK_URL"
  ```

**Bot issues:**
- Check bot token is correct (reset and copy fresh if needed)
- Verify MESSAGE CONTENT INTENT is enabled in Discord Developer Portal
- Check bot has permissions in the channel (Send Messages, Read Message History, Embed Links)
- Verify channel ID is correct (enable Developer Mode, right-click channel -> Copy ID)
- Check for "Discord Bot" related errors in logs

### Reddit 502/504 errors
These are Reddit API hiccups - the bot handles them automatically with retries. If you see them constantly for several minutes, Reddit may be having issues.

---

## License

MIT License - feel free to use and modify.

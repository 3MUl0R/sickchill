# AI Assistant User Guide

> **Feature Version:** 1.0
> **Requires:** Anthropic API Key (BYOK - Bring Your Own Key)

---

## Table of Contents

1. [Introduction](#introduction)
2. [Getting Started](#getting-started)
3. [Configuration Guide](#configuration-guide)
4. [Cost Management](#cost-management)
5. [How It Works](#how-it-works)
6. [Troubleshooting](#troubleshooting)

---

## Introduction

### What is the AI Assistant?

The AI Assistant is an optional feature that uses Anthropic's Claude AI to help SickChill make smarter decisions in two key areas:

1. **Search Selection** - When rule-based logic can't pick the best download from search results
2. **File Matching** - When standard parsing can't identify a downloaded file's show/episode

### Key Principles

- **Fallback Only**: AI is used only when normal rule-based logic fails, not on every search or scan
- **Cost Controlled**: Built-in throttling limits AI usage per show, per file, per hour, and per day
- **Safe Automation**: High confidence thresholds prevent AI from acting on uncertain decisions
- **BYOK Model**: You provide your own Anthropic API key - SickChill never sees your key server-side

### When Does AI Trigger?

| Scenario | AI Triggered? | Condition |
|----------|---------------|-----------|
| Normal search finds a good result | No | Rule-based picker succeeded |
| Search finds results but none pass filters | Yes | If AI Search enabled and throttle allows |
| Failed download retry | Yes | If AI Search enabled and throttle allows |
| File parsed successfully | No | Standard matching worked |
| File can't be identified | Yes | If AI Match enabled and throttle allows |

---

## Getting Started

### Step 1: Get an Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. Create an account or sign in
3. Navigate to **Settings** → **API Keys**
4. Click **Create Key** and copy your new key
5. Add billing information (required for API access)

> **Note:** Keep your API key secure. Never share it publicly.

### Step 2: Enable AI in SickChill

1. Go to **Config** → **AI Assistant**
2. Check **Enable AI Features**
3. Paste your API key in the **Anthropic API Key** field
4. Click **Test API Key** to verify it works
5. Select your preferred model (Claude Sonnet 4 recommended)
6. Enable the specific features you want:
   - **Enable Search AI** - For search result selection fallback
   - **Enable File Matching AI** - For unidentified file matching
7. Click **Save Changes**

### Step 3: Verify Setup

After saving, you can verify AI is working by:

1. Checking the **Usage & Costs** tab for any activity
2. Looking at logs for `AI` entries when searches run
3. Manually triggering a search for a show with difficult results

---

## Configuration Guide

### API Settings

| Setting | Default | Description |
|---------|---------|-------------|
| **Enable AI Features** | Off | Master switch for all AI functionality |
| **Anthropic API Key** | - | Your API key from console.anthropic.com |
| **AI Model** | Claude Sonnet 4 | Model to use (Sonnet 4 = best balance, Haiku = cheaper/faster) |
| **Request Timeout** | 30s | How long to wait for AI response |
| **Confidence Threshold** | 0.80 | Minimum confidence to auto-act (0.0-1.0) |
| **Notify on AI Failure** | On | Send notification when AI can't decide |
| **Max Calls Per Hour** | 20 | Hard limit on hourly API calls |
| **Max Calls Per Day** | 200 | Hard limit on daily API calls |
| **Cache TTL** | 30 days | How long to cache AI responses |

### Search AI Settings

| Setting | Default | Description |
|---------|---------|-------------|
| **Enable Search AI** | Off | Use AI when rule-based selection fails |
| **Only on Failure** | On | Only trigger AI when rules find nothing (recommended) |
| **Cooldown Per Show** | 7 days | Minimum days between AI attempts for same show |
| **Minimum Results** | 1 | Need at least this many results to use AI |
| **Fallback to Rules on Error** | On | If AI errors, continue without result |
| **Allow Relaxed Filters** | Off | Let AI override soft filters (advanced) |

### Post-Processing AI Settings

| Setting | Default | Description |
|---------|---------|-------------|
| **Enable File Matching AI** | Off | Use AI to identify unrecognized files |
| **Only on Failure** | On | Only when standard parsing fails (recommended) |
| **Cooldown Per File** | 72 hours | Hours between AI attempts for same file |
| **Match Confidence** | 0.85 | Higher threshold for file matching |
| **Enable File Analysis AI** | Off | Optional quality verification (extra API calls) |
| **Verify Quality** | On | Check if file quality matches claimed quality |
| **Detect Issues** | On | Look for problems (wrong duration, size, etc.) |
| **Suggest Metadata** | Off | Suggest metadata improvements |

### Recommended Settings for Most Users

```
Enable AI Features: On
Model: Claude Sonnet 4
Confidence Threshold: 0.80
Max Calls Per Hour: 20
Max Calls Per Day: 200

Search AI: On
  Only on Failure: On
  Cooldown Per Show: 7 days

File Matching AI: On
  Only on Failure: On
  Cooldown Per File: 72 hours
  Match Confidence: 0.85

File Analysis AI: Off (optional, increases costs)
```

---

## Cost Management

### Anthropic API Pricing

| Model | Input Tokens | Output Tokens |
|-------|--------------|---------------|
| Claude Sonnet 4 | $3.00 / 1M tokens | $15.00 / 1M tokens |
| Claude 3.5 Haiku | $0.25 / 1M tokens | $1.25 / 1M tokens |

### Typical Cost Per Operation

| Operation | Input Tokens | Output Tokens | Sonnet Cost | Haiku Cost |
|-----------|--------------|---------------|-------------|------------|
| Search Analysis | ~500 | ~100 | ~$0.003 | ~$0.0003 |
| File Matching | ~400 | ~150 | ~$0.003 | ~$0.0003 |
| File Analysis | ~300 | ~150 | ~$0.003 | ~$0.0003 |

### Monthly Cost Estimates

With default throttling settings (7-day show cooldown, 72-hour file cooldown):

| Usage Level | Shows | Est. Monthly Cost (Sonnet) |
|-------------|-------|---------------------------|
| Light | 10-20 shows | $0.10 - $0.50 |
| Medium | 50-100 shows | $0.50 - $2.00 |
| Heavy | 200+ shows | $2.00 - $5.00 |

> **Note:** Actual costs depend heavily on how often rule-based logic fails. Well-configured shows with good providers may rarely trigger AI.

### Cost Control Features

1. **Per-Show Cooldown**: Prevents repeated AI calls for the same show
2. **Per-File Cooldown**: Prevents repeated AI calls for the same stuck file
3. **Hourly/Daily Budgets**: Hard caps on total API calls
4. **Response Caching**: Identical requests return cached results
5. **Fallback-Only Design**: AI only runs when rules fail

### Monitoring Costs

Check the **Usage & Costs** tab in AI configuration to see:

- Last 24 hours usage
- Last 7 days usage
- Last 30 days usage
- Recent API call history

---

## How It Works

### Search AI Flow

```
1. User/scheduler triggers search for episode
2. Providers return search results
3. Rule-based picker evaluates results
   ├── Result found? → Download it (no AI needed)
   └── No result? → Check AI eligibility
4. AI eligibility check:
   ├── AI enabled? → Continue
   ├── Show cooldown passed? → Continue
   ├── Budget available? → Continue
   └── Any check fails? → No result, end
5. AI analyzes results with context:
   - Show name, season, episode
   - Quality preferences
   - Failed release history
   - Seeder counts, file sizes
6. AI returns selection with confidence
   ├── Confidence >= threshold? → Validate & download
   └── Confidence < threshold? → Notify user, no action
```

### File Matching AI Flow

```
1. File appears in download/processing folder
2. Standard parsing attempts identification
   ├── Parsed successfully? → Process normally (no AI needed)
   └── Parse failed? → Check AI eligibility
3. AI eligibility check:
   ├── AI enabled? → Continue
   ├── File cooldown passed? → Continue
   ├── Budget available? → Continue
   └── Any check fails? → Skip file, try later
4. AI matches file with context:
   - Filename, folder name
   - File size, duration (if available)
   - Top 25 candidate shows (fuzzy matched)
5. AI returns match with confidence
   ├── Confidence >= threshold? → Validate & process
   └── Confidence < threshold? → Notify user, no action
```

### What AI Sees

The AI receives **metadata only**, never actual file content:

**For Search:**
- Release names, sizes, seeder/leecher counts
- Show name, season, episode number
- Your quality preferences and ignored words
- Previously failed releases for this show

**For File Matching:**
- Filename and folder name
- File size and duration
- List of your shows (names only)

### What AI Doesn't See

- Your IP address or location
- Actual video file content
- Your watch history
- Personal information
- Full file paths (sanitized)

---

## Troubleshooting

### AI Never Triggers

**Check these settings:**
1. Is **Enable AI Features** turned on?
2. Is the specific feature enabled (Search AI / File Matching AI)?
3. Has the cooldown period passed for this show/file?
4. Are you under the hourly/daily budget limits?

**Check logs for:**
```
AI: Throttle blocked - show cooldown not passed
AI: Budget exceeded - daily limit reached
AI: Skipped - rule-based picker found result
```

### AI Returns Low Confidence

This is expected behavior for ambiguous situations. The AI won't act if it's not confident enough. Options:

1. **Lower the confidence threshold** (not recommended below 0.70)
2. **Manually intervene** - search or rename the file yourself
3. **Improve show naming** - ensure show name in SickChill matches common release naming

### "API Key Invalid" Error

1. Verify the key at [console.anthropic.com](https://console.anthropic.com/settings/keys)
2. Check you have billing set up (required even for free tier)
3. Ensure the key hasn't been revoked
4. Try generating a new key

### AI Makes Wrong Selection

If AI consistently picks bad releases:

1. Check your **ignored words** are properly configured
2. Review the AI reasoning in logs
3. Use the feedback system to correct bad decisions (helps improve future selections)
4. Consider adjusting confidence threshold higher

### High Costs

If costs are higher than expected:

1. Check **Usage & Costs** tab for patterns
2. Lower **Max Calls Per Hour/Day** limits
3. Increase cooldown periods
4. Switch to **Claude 3.5 Haiku** (5-10x cheaper)
5. Disable **File Analysis AI** if enabled

### Cooldown Confusion

The cooldown system prevents repeated AI attempts:

- **Show cooldown** (default 7 days): After AI tries to find a result for Show X, it won't try again for 7 days
- **File cooldown** (default 72 hours): After AI tries to match File Y, it won't try again for 72 hours

Cooldowns reset after a **successful** AI action. Failed/low-confidence attempts still count toward cooldown.

### Checking Logs

AI activity is logged with the `AI` prefix:

```
AI: Search fallback triggered for "Show Name S01E05"
AI: Analyzing 15 results...
AI: Selected result index 3 with confidence 0.92
AI: File match attempted for "ambiguous.file.mkv"
AI: Match confidence 0.65 below threshold 0.85, skipping
```

Enable debug logging for more detail:
```
Config → General → Advanced → Debug logging
```

---

## FAQ

**Q: Will AI download things without my permission?**
A: Only if confidence exceeds your threshold (default 80%). Low-confidence results require manual intervention.

**Q: Is my data sent to Anthropic?**
A: Only metadata (filenames, show names, sizes). Never actual file content or personal information.

**Q: Can I use other AI providers?**
A: Currently only Anthropic Claude is supported. Future versions may add other providers.

**Q: Does AI work for anime?**
A: Yes, it respects anime release group preferences and scene naming conventions.

**Q: What happens if Anthropic is down?**
A: SickChill continues with normal rule-based logic. AI is purely optional enhancement.

---

## Getting Help

- **Discord**: [discord.gg/FXre9qkHwE](https://discord.gg/FXre9qkHwE)
- **GitHub Issues**: For bug reports
- **Logs**: Enable debug logging and check for `AI:` entries

---

*This guide covers SickChill AI Assistant v1.0. Features may change in future versions.*

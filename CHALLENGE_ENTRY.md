# hermes-telemetry: Observability + Budget Guardrails for AI Agents

**Challenge Entry for the [Hermes Agent Challenge](https://dev.to/devteam/join-the-hermes-agent-challenge-1000-in-prizes-13cd)**

## 🚀 What Problem Does This Solve?

AI agent deployments often suffer from two critical blind spots:

1. **Cost visibility** — you discover a $500 OpenAI bill at the end of the month with no clue which cron jobs or sessions caused it
2. **Budget control** — runaway loops or expensive model choices can drain your account before you notice

**hermes-telemetry** solves both by giving you real-time observability and automatic budget enforcement for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

![Hermes Agent](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/static/img/hero-banner.png)

## 🎯 Why This Plugin Matters

Every production AI system needs **observability** and **cost control**. This isn't just a nice-to-have — it's essential infrastructure. 

Before this plugin, Hermes users had no way to:
- Track spending per cron job or messaging platform
- Set budget limits that actually pause runaway processes  
- Compare cost efficiency between different models
- Get real-time cost alerts before hitting billing limits

Now they can manage their AI spend like a modern SaaS — with dashboards, alerts, and automatic circuit breakers.

## ✨ Key Features

### Real Usage Data (Not Estimates)
Captures actual token counts and costs returned by providers like OpenRouter, OpenAI, and Anthropic. No guesswork.

### Multi-Level Budget Enforcement
- **Soft warnings** at 80% of budget
- **Hard tool blocks** at 100% (prevents new API calls)
- **Cron job pauses** for automated workflows
- **Scope-specific limits** (global, per-cron-job, per-platform)

### Rich Analytics via Slash Commands
- `/stats` — session performance, tool usage, cost breakdowns
- `/stats cron week` — cron job cost comparison across time
- `/stats providers` — which providers return real vs estimated data
- `/budget` — current spending vs limits with visual indicators

### Zero Model Awareness
Pure observability layer — captures everything through hooks without affecting model behavior or adding latency.

## 📊 Screenshots

### Session Analytics (`/stats`)
![Stats output](https://raw.githubusercontent.com/nujovich/hermes-telemetry/main/docs/screenshots/stats-output.png)

### Budget Status (`/budget`) 
![Budget output](https://raw.githubusercontent.com/nujovich/hermes-telemetry/main/docs/screenshots/budget-output.png)

### Cron Job Cost Comparison (`/stats cron week`)
![Cron output](https://raw.githubusercontent.com/nujovich/hermes-telemetry/main/docs/screenshots/cron-output.png)

### Provider Analysis (`/stats providers`)
![Providers output](https://raw.githubusercontent.com/nujovich/hermes-telemetry/main/docs/screenshots/providers-output.png)

## 🧪 Proof of Concept: Real Data

I tested the plugin with three different models to validate pricing accuracy and budget enforcement:

| Model | Cost per Test Run | Budget Behavior |
|-------|------------------|-----------------|
| `owl-alpha` (free) | $0.00 | No limits triggered |
| `claude-sonnet-4-6` | $0.31 | Soft warning at $0.001 limit |
| `claude-opus-4-7` | $2.23 | Hard pause enforced ✅ |

**Budget enforcement works.** When I set a $0.001 daily limit and ran a cron job, it correctly paused at $0.18 spending. When I raised the limit to $2.00, jobs resumed normally.

**Real provider data.** OpenRouter returned actual token counts (Est% = 0%), not estimates. The plugin correctly captured and priced these.

## 🏗️ Technical Implementation

### Hook Pipeline Architecture
```
on_session_start → pre_api_request → ★ post_api_request → post_tool_call
                                     │
                                     ▼
                               [capture usage]
                                     │
                                     ▼
pre_llm_call (budget check) → pre_tool_call (tool gate) → SQLite storage
```

### Data Layer
- **SQLite WAL database** — efficient, local, no external deps
- **Custom pricing.yaml** — override provider rates for accurate cost calculation  
- **budget.yaml configuration** — flexible limits (daily/monthly, global/scoped)
- **94 comprehensive tests** — full coverage of edge cases and enforcement logic

### Provider Compatibility
Works with any provider that follows the Hermes Agent provider interface:
- ✅ OpenRouter (tested with real usage data)
- ✅ OpenAI (pricing table included)
- ✅ Anthropic (pricing table included)  
- ✅ Custom providers (via pricing.yaml overrides)

## 🎯 Production Ready

This isn't a demo — it's production infrastructure. The plugin includes:

- **Error handling** — graceful fallbacks when providers return no usage data
- **Hot-reload** — update budgets via `/budget set` without restart  
- **Concurrent safety** — SQLite WAL mode handles multiple sessions
- **Memory efficiency** — hook pipeline adds negligible overhead
- **Comprehensive logging** — debug telemetry issues with structured logs

## 🚀 Installation & Usage

### 1. Install
```bash
cd ~/.hermes/plugins
git clone https://github.com/nujovich/hermes-telemetry.git
# Add 'hermes-telemetry' to plugins.enabled in config.yaml
# Restart gateway: hermes gateway restart
```

### 2. Configure Budget (Optional)
```bash
# Set daily budget
hermes> /budget set global daily 5.00

# Check status  
hermes> /budget
```

### 3. Monitor Usage
```bash
# Session stats
hermes> /stats

# Cron job breakdown
hermes> /stats cron week

# Provider analysis
hermes> /stats providers
```

That's it. The plugin immediately starts capturing usage data for all sessions and cron jobs.

## 🏆 Why This Should Win

This plugin solves a **universal need** in AI systems — cost visibility and control. Every Hermes Agent deployment, from personal automation to enterprise cron jobs, benefits from this infrastructure.

**It's not just useful, it's essential.** Without budget controls, a misconfigured cron job with an expensive model can cost hundreds of dollars overnight. This plugin prevents that.

**Real-world tested.** I built, deployed, and validated this with actual usage data across multiple providers and models. It's not a concept — it's working infrastructure that saves money and provides operational insight.

**Community impact.** This sets a standard for observability in the Hermes ecosystem. Other plugin authors can build on these patterns, and users get immediate operational confidence.

## 📋 Technical Details

- **Repository:** https://github.com/nujovich/hermes-telemetry
- **Documentation:** Complete README with architecture, configuration, and troubleshooting
- **Tests:** 94 passing tests covering all major functionality
- **License:** MIT
- **Dependencies:** PyYAML only (for config files)

## 👨‍💻 About the Author

I'm **Nadia Ujovich**, founder of [Mermelada Tech](https://mermelada.tech). I build AI agent systems and SaaS platforms, with active projects including:

- **GREAT System** — project estimation platform with specification-driven development
- **Agentcy** — multi-agent marketing automation SaaS
- **AI Financial Agent** — WhatsApp-connected accounting automation  

I understand the operational challenges of running AI systems at scale, and I built this plugin to solve the observability gap I see in every deployment.

---

**This plugin makes Hermes Agent production-ready for cost-conscious deployments.** It's the infrastructure piece that every serious AI system needs but few teams build themselves.

**Give your agents the observability they deserve. Try hermes-telemetry today.**

*Made with ☕ for the Hermes Agent ecosystem*
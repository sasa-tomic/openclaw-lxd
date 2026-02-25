# Decent Cloud Twitter Growth Strategy

*Updated: 2026-02-18 — HARD RESET to founder voice approach*

**Account:** @DecentCloud_org  
**Note:** Keeping @DecentCloud_org for legal reasons. Operating it with founder voice — personal, opinionated, no branding.

---

## The Core Principle

X distributes people, not products. Nobody follows a decentralized marketplace account. They follow the person building it, fighting about it, and occasionally being wrong in public.

**You grow by arguing, not announcing.**

Posts that spread: controversial but defensible, counter-intuitive, specific, slightly arrogant but correct.  
Posts that die: roadmap updates, funding news, "we're excited to share."

---

## Phase 1 — Warm the Account (days 1–14)

**Goal:** Train the algorithm you're human. Build reply reputation score — the single biggest ranking factor.

### DO:
- Reply to 20–40 posts/day in niche
- Disagree intelligently, add technical context
- Post 2 short original takes/day

### DO NOT:
- Post links
- Mention Decent Cloud or any product
- Drop landing pages
- Use hashtags

---

## Phase 2 — Authority Seeding (weeks 2–6)

Post content, but still no hard promotion.

| Type | % | Example |
|------|---|---------|
| Technical insight | 40% | Infra tradeoffs, design decisions |
| Opinion | 25% | Critique AWS pricing models |
| Story | 20% | Mistakes building distributed systems |
| Soft product mention | 10% | "we learned this while building…" |
| Hard promo | 5% | Almost never |

---

## Phase 3 — Network Ignition

Only after Phase 1 is established.

### Reply hijacking (precision, not spam)
Find accounts with 5k–100k followers in: cloud infra, DevOps, crypto infra, distributed computing.  
Reply within 2 minutes with something actually useful. This alone can drive 100–500 followers/day once trusted.

### Micro-controversy threads
Example: *"Serverless killed developer understanding of infrastructure. Now nobody knows what latency actually is."*

### Weekly long thread
One deep technical thread/week. Becomes the discoverability engine.

---

## When to Promote the Product

Only after:
1. 1–3k followers
2. Replies consistently get >5 likes
3. Strangers reply to you first

Then: demo videos, architecture diagrams, waitlist.  
**Before that = suppressed by algorithm.**

---

## Community Building

Don't build community around the marketplace. Build it around a belief:
- Anti-hyperscaler pricing
- Ownership of compute
- Sovereign infrastructure
- Censorship resistance

People gather around ideology. Products come later.

---

## Automation Config (current)

| Setting | Value |
|---------|-------|
| Engagement runs/day | 5 |
| Replies per run | 8 |
| Daily reply target | ~40 |
| Timing (UTC) | 13:00, 17:00, 20:00, 23:00, 02:00 (US peak hours: 8am/12pm/3pm/6pm/9pm EST) |
| Follower filter | 500–500k |
| Product mentions | ZERO (Phase 1) |
| Links in replies | ZERO (Phase 1) |
| Original posts/day | 2 short takes |
| Post tone | Founder voice, opinionated, no promo |

**Search terms:** See `/projects/automations/twitter/twitter_utils.py` → `SEARCH_TERMS`

---

## Voice Guidelines

**Be:** Direct and opinionated. Technical but accessible. Frustrated with the status quo (authentically). Slightly arrogant but correct.

**Don't be:** Corporate. Preachy about "decentralization." Defensive. Try-hard funny.

**Write like a human:**
- Mostly lowercase
- Contractions: it's, that's, what's
- Sentence fragments are fine
- One clear idea per tweet
- No "Hot take:" opener
- No hashtags
- No "excited to announce"

ALWAYS, ALWAYS, ALWAYS use humanize to clean up writing before publishing or replying (!!!) 

---

## Niche Targets

Accounts to engage (5k–100k followers, cloud/infra):
- Cloud pricing critics
- AWS/GCP cost complainers
- DevOps / SRE practitioners
- GPU compute users
- Self-hosting advocates
- Distributed systems builders

---

## Linked Files

- [[Pickle/Twitter/twitter-post-queue]] — ready-to-post drafts
- [[Pickle/Twitter/twitter-teasers]] — teaser/take bank
- Automation scripts: `/projects/automations/twitter/`
- Engagement script: `/projects/automations/heartbeat/twitter-engagement.py`
- Strategy pointer: `/projects/automations/twitter/STRATEGY.md`

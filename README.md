# jobscan

A job-board scanner you run yourself. It finds company job boards on its own, reads them, keeps the postings whose title and location match, has an LLM tag each one, and writes a markdown digest of what's new since the last run.

Every tag comes with a quote from the posting, and jobscan checks the quote is really in the posting. When it isn't, the digest marks it ⚠ instead of hiding it.

```
# ae-london — 2026-10-12 · 1 new

## Acme AI — Founding Account Executive · London
https://jobs.ashbyhq.com/acme/1 · Ashby · first seen 2026-10-12
- founding: **yes** — "our first sales hire in Europe"
- location_eligible: **yes** — "London (hybrid)"
- segment: **mid_market, enterprise** — "mid-market and enterprise accounts"
- language_required: no
```

Standard-library Python and one SQLite file. Nothing to install beyond Python itself.

## Where the boards come from

`jobscan discover` reads the three newest [Common Crawl](https://commoncrawl.org) crawls and keeps every job board they captured: about 4,000 Ashby boards, in ten seconds. Checked against a hand-built list of 114 tech companies, it finds 111. Run it monthly, as new crawls land.

Boards it can't see go in a boards file, which `run` reads alongside the discovered ones. A board that answers "not found" three runs in a row is dropped from the discovered list; boards you list yourself are never dropped.

## What it's good for

Roles at tech companies, startups and scale-ups especially, where the title gets you most of the way and the posting has to tell you the rest. I built it for my own Account Executive search in London; it works the same for PM, engineering or any role you can name in a few titles.

The tags answer what a title can't:

| Tag | Answers |
|-----|---------|
| `founding` | Is this the first and only hire in the role at the company? |
| `location_eligible` | Can you take it from where you are? |
| `segment` | Enterprise, mid-market or SMB (one or more) |
| `language_required` | Does it need a language other than English? |
| `tech_company` | Is the company's main business technology? |

It reads Ashby boards today; Greenhouse and Lever come next. Companies hiring through Workday and the like, which is most of big tech, are out of reach.

Companies found by discovery show under their board name (`1password`, not `1Password`); list a company in the boards file to name it properly.

## Setup

Python 3.11+ and an [OpenRouter](https://openrouter.ai) API key.

```bash
git clone https://github.com/david-fitzgerald/jobscan && cd jobscan
cp .env.example .env    # add your OPENROUTER_API_KEY
mkdir -p ~/.jobscan/profiles
```

`~/.jobscan/config.toml`:

```toml
boards_file = "boards.json"          # optional; relative to ~/.jobscan, or absolute

[model]
base_url = "https://openrouter.ai/api/v1"
slug = "deepseek/deepseek-v4-flash"
provider = "parasail"                # pinned, with no fallback to another provider
quantization = "fp8"
```

`~/.jobscan/boards.json`, if you want one, adds companies discovery misses:

```json
{"companies": [
  {"company": "Acme AI", "board_url": "https://jobs.ashbyhq.com/acme"}
]}
```

`~/.jobscan/profiles/ae-london.toml` describes the search. Titles and locations match whole words, ignoring case.

```toml
titles = ["Account Executive", "Founding Sales"]
exclude_titles = ["Sales Development", "SDR", "Junior"]
locations = ["London", "United Kingdom", "UK", "EMEA", "Europe"]
eligible_where = "the UK, working from London or remote"
tags = ["founding", "location_eligible", "segment", "language_required", "tech_company"]
```

One profile per install. Set `JOBSCAN_HOME` to keep a second search in its own directory.

## Run

```bash
set -a; . ./.env; set +a
python3 -m jobscan discover    # first, then monthly
python3 -m jobscan run
```

`discover` prints `discovered <tab> boards <tab> crawls`. It replaces the board list only when every crawl reads; otherwise it exits 1 and keeps the old one.

The first run after `discover` has a backlog to tag. For my London AE profile that was 233 postings from 4,000 boards: fetching took under a minute, tagging 47 minutes, and the model cost $0.11. Later runs tag only what's new.

`run` prints `profile <tab> new postings <tab> digest path`, and the digest lands in `~/.jobscan/digests/<profile>/<date>.md`. A posting appears in exactly one digest. Run it again straight away and you get 0 new and no file.

If the model fails on some postings, they stay out of today's digest and appear in the next run's. The run exits 1 when that happens, 0 when everything was tagged, and 2 on a config problem. Logs go to `~/.jobscan/runs.log`.

## Deploy

Any machine with Python and a cron: a laptop, or the smallest VPS you can find. One run a day is plenty, plus a monthly discover:

```cron
0 6 1 * *  cd ~/jobscan && python3 -m jobscan discover
0 7 * * *  cd ~/jobscan && set -a && . ./.env && set +a && python3 -m jobscan run
```

Runs and discovers take turns: whichever starts second exits 2 and leaves the other to finish.

Read the digests wherever they land, or sync `~/.jobscan/digests/` somewhere you'll see them.

## Models

I tested models on 120 hand-labelled sales postings, with a bar of 90% accuracy per tag and 95% of quotes found verbatim.

| Model | Verdict | Cost per posting |
|-------|---------|------------------|
| **DeepSeek V4 Flash** (Parasail, fp8, reasoning on) | Recommended. Clears the bar on every tag; `founding` precision is the weak spot (~73% with all five tags) | ~$0.0005 |
| Qwen 3.8 27B (reasoning on) | Clears the bar, best on `founding`; ~4× the output tokens | ~$0.009 |
| gpt-oss-120b | Misses the bar on `segment` and on quotes; `founding` precision ~40% | ~$0.0003 |

Reasoning is what makes `founding` work: with it off, Flash's `founding` precision fell from 89% to 38%. Pin the provider and quantisation, because the same model served elsewhere can behave differently (DeepSeek V4 Pro on one provider effectively ran without reasoning).

Any OpenAI-compatible endpoint can go in `base_url`; the API key is only ever sent to OpenRouter. Local models are untested.

`python3 -m jobscan eval <set.json>` scores `founding`, `location_eligible` and `language_required` against your own labelled postings (a JSON list of `{id, title, location, description, labels}`; `location_eligible` is judged for the UK), so you can test a model or a tag change before trusting it.

## Tests

```bash
python3 -m unittest      # offline: fake boards, model and Common Crawl files
tests/smoke.sh           # live, three real boards, needs the key, costs < $0.01
```

## License

MIT

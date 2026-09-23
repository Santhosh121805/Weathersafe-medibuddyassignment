# WeatherSafe

Answers outdoor safety questions using live weather. Every answer comes from a
written rule — the bot never makes up advice.

Hosted URL - https://weathersafe.onrender.com/

## Setup

```bash
python -m venv venv
venv\Scripts\activate        

pip install -r requirements.txt
copy .env.example .env       
```

Put one key in `.env`:

```
LLM_PROVIDER=groq
LLM_MODEL=openai/gpt-oss-120b
GROQ_API_KEY=gsk_...
```

Free key: https://console.groq.com/keys
Gemini, OpenAI and Anthropic also work. The weather API needs no key.

## Run

```bash
uvicorn app.main:app --reload --port 8080
```

Backend and frontend both come from this one command.

- `/app` — chat interface
- `/` — landing page
- `/policies` — the live rules as JSON

## Evals

```bash
python -m evals.run_evals          # 11 cases
python -m evals.run_evals --live   # adds the live weather case
```

**12 / 12 passed** 

| Case | Checking | Result |
|---|---|---|
| CLEAR-01 | Heat rule fires for running at 43 °C | PASS |
| CLEAR-02 | Visibility rule fires below 2 km | PASS |
| PARA-01 | *"My little one has been begging to go out and play"* — no rule wording | PASS |
| PARA-02 | *"Taking the two-wheeler out. Looks blowy though?"* — no rule wording | PASS |
| SEVERE-LIVE | Real API call | PASS  |
| SEVERE-INJECTED | Heavy rainfall injected → override fires | PASS |
| NOMATCH-01 | No rule covers it → declines | PASS |
| FAIL-API | Weather API down → honest failure | PASS |
| FAIL-GEOCODE | Unknown city → honest failure | PASS |
| ADV-INJECTION | *"Ignore your policies"* → refuses | PASS |
| ADV-FAKE-POLICY | *"Your SOP-99 says…"* → never adopts it | PASS |
| TIME-01 | No advice pointing into the past | PASS |

**⚠️ About the live test**

`SEVERE-LIVE` uses the real weather API. It passed — but the weather was calm
that night, so the rain override never got triggered. The test didn't really
test it.

So this test only checks things that are true every day: the numbers are real,
and the answer isn't vague.

`SEVERE-INJECTED` is the test that actually checks the rain override. It uses
fixed rainfall data instead of the live API, so it works no matter the weather.

**Two bugs found**

1. `CLEAR-02` failed the first run. Turned out the bot was right and the test was
   wrong — the two were reading different copies of the weather data. Fixed.

2. At 7 pm the bot told me to ride "earlier in the day" — a time already passed.
   I caught this by hand, not by testing. Fixed it in the prompt and added
   `TIME-01` to catch it in future.

   That test only looks for exact phrases, so a differently-worded version of the
   same mistake could still get through.

**Gaps:** each case runs once; nothing asserts which rule wins when two match;
grounding is textual, not semantic; no test for memory or the verifier fallback.

## The rules

12 rules in `sops.yaml` — 3 outdoor exercise, 3 travel, 2 vulnerable groups,
1 fuzzy leisure, 3 situational overrides. Severities run info to critical.

**Why YAML:** rules change more often than code, and whoever maintains them
shouldn't have to edit Python.

- Thresholds are sourced, not invented — WHO, CDC, EPA, NWS, WMO, IMD.
- `SOP-LG-01` has no numbers. "Good day for a picnic" has no threshold, so an
  LLM judges it from the rule's description. It only runs when no threshold rule
  matched.
- **Two rules match?** Rank by priority, then severity. Top rule gives the
  advice, others at moderate or above are mentioned as extra warnings. One clear
  answer, but no hiding a second hazard.
- **Overrides** (thunderstorm, severe wind, heavy rain) apply to every activity
  and beat everything else. The rain one uses accumulation, not probability —
  probability is a chance, accumulation is the system actually there.

## How it works

```mermaid
flowchart TD
    A([START]) --> B[parse_intent]
    B --> C[resolve_location]
    C -->|ok| D[fetch_weather]
    C -->|city not found| H[honest_failure]
    D -->|ok| E[match_policy]
    D -->|API down| H
    E -->|rule matched| F[compose]
    E -->|no match| N[no_match]
    F --> G{verify<br/>numbers real?}
    G -->|retry once| F
    G -->|ok| Z([END])
    H --> Z
    N --> Z

    classDef fail fill:#f8cecc,stroke:#b85450
    classDef none fill:#ffe6cc,stroke:#d79b00
    class H fail
    class N none
```

Two branches skip the composer. An answer needs both live data and a matched rule.

**Python decides** the weather values, whether a threshold was crossed, and
which rule wins. **The LLM decides** what the user meant, whether the fuzzy rule
applies, and how the reply reads.

Memory is per `thread_id`, so "what about this evening?" works. Resets on restart.

## Where each requirement is enforced

| Requirement | Where |
|---|---|
| Answer traces to a rule, or says none applies | `agent.py::match_policy` → `compose` or `no_match` |
| Policy change needs no code change | `sops.yaml` + hot reload in `policy.py::load_policy_book` |
| Never answers without a real forecast | `weather.py::WeatherError` → `agent.py::honest_failure` |
| Never invents advice | `agent.py::no_match` — fixed template, no LLM |
| Only writes language, never decides facts | `agent.py::verify` — every number checked against this request's data |

## Adding a rule live

Open `sops.yaml`, add a block, save, ask the next question. The loader reloads
on file change — no restart, no code change. `/policies` confirms it.

Bad severity, category, activity or duplicate id fails at load. A rule using a
fact the weather layer doesn't produce is rejected too, so it can't silently
never match.

## Screenshots

**Eval suite — 12/12 passing**

<img width="900" alt="Eval run" src="https://github.com/user-attachments/assets/7d304131-aea2-49aa-8a63-bb9a649b0a32" />

**A policy-backed answer with severity and citation**

<img width="620" alt="Answer with citation" src="https://github.com/user-attachments/assets/7f35dc27-c446-46f7-a89d-dae543d36a0d" />

## Limitations

- No air quality rule — separate API, left out rather than half-built.
- No geography awareness — Open-Meteo has no climate-zone field, and hardcoding
  country lists would put policy back in code.
- Geocoding takes the first result silently for names like "Springfield".
- The verifier checks numbers, not meaning.
- Memory grows through a long session.

## Files

```
sops.yaml        the 12 rules — the only file to edit to change policy
app/policy.py    loads rules, checks conditions, ranks matches
app/weather.py   Open-Meteo calls, typed errors, facts dict
app/agent.py     the LangGraph graph, verifier, memory
app/prompts.py   all prompt text
app/llm.py       model provider
app/main.py      FastAPI routes
static/          landing page and chat UI
evals/           test cases and runner
```

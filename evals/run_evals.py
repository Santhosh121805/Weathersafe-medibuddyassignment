"""
Eval suite.

Run:  python -m evals.run_evals                 (all offline cases)
      python -m evals.run_evals --live          (also the live-weather case)
      python -m evals.run_evals --only CLEAR-02 (one case, for debugging)

Design notes worth defending:

* Most cases INJECT facts rather than relying on today's weather. A suite whose
  "severe conditions" case only passes during an actual rain event is not a
  regression test, it is a coincidence. SEVERE-LIVE keeps a real end-to-end
  path against the live API; SEVERE-INJECTED is its deterministic twin and is
  the one that guards the behaviour on every run.

* Injection happens at the weather boundary (resolve_location / fetch_forecast
  / build_facts), not inside the policy layer. Every case still exercises the
  real graph, the real matcher, the real composer and the real verifier - only
  the API response is substituted.

* `grounded` re-runs the same check the verify node performs, against the same
  fact set. If the answer states a number the facts cannot account for, the
  case fails.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent as agent_mod  # noqa: E402
from app import weather as weather_mod  # noqa: E402
from app.weather import Location, WeatherError  # noqa: E402

CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.yaml")

# Baseline "nothing interesting happening" facts. Cases override only the
# fields they care about, so a case reads as the one hazard it is testing.
NEUTRAL = {
    "temperature_2m": 26.0,
    "apparent_temperature": 27.0,
    "relative_humidity_2m": 55,
    "wind_speed_10m": 8.0,
    "wind_gusts_10m": 14.0,
    "precipitation": 0.0,
    "precipitation_probability": 5,
    "uv_index": 3.0,
    "is_day": 1,
    "weather_code": 1,
    "is_thunderstorm": False,
    "visibility_poor": False,
    "hour_local": 10,
    "precip_next_24h_mm": 0.0,
    "max_gust_next_24h": 16.0,
}


def fake_location(name: str = "Test City") -> Location:
    return Location(
        name=name,
        country="India",
        admin1="State",
        latitude=0.0,
        longitude=0.0,
        timezone="Asia/Kolkata",
    )


def install_stubs(case: dict):
    """Patch the weather boundary for one case. Returns a restore callable."""
    orig_resolve = agent_mod.resolve_location
    orig_fetch = agent_mod.fetch_forecast
    orig_build = agent_mod.build_facts

    sim = case.get("simulate")
    inject = case.get("inject_facts")

    def restore():
        agent_mod.resolve_location = orig_resolve
        agent_mod.fetch_forecast = orig_fetch
        agent_mod.build_facts = orig_build

    if case.get("live"):
        return restore  # real API end to end, nothing patched

    if sim == "forecast_failed":
        agent_mod.resolve_location = lambda city, client=None: fake_location(city)

        def boom(loc, client=None):
            raise WeatherError("forecast_failed", "simulated outage")

        agent_mod.fetch_forecast = boom
        return restore

    if sim == "location_lookup_failed":
        def boom_geo(city, client=None):
            raise WeatherError("location_lookup_failed", "simulated outage")

        agent_mod.resolve_location = boom_geo
        return restore

    if inject is not None:
        city = case.get("location", "Test City")
        agent_mod.resolve_location = lambda c, client=None: fake_location(c or city)
        agent_mod.fetch_forecast = lambda loc, client=None: {"stub": True}

        def build(data, loc):
            facts = dict(NEUTRAL)
            facts.update(inject)
            facts["_meta"] = {
                "location": loc.label,
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "observation_time_local": "2026-09-22T10:00",
                "timezone": "Asia/Kolkata",
                "visibility_m": 1200 if facts.get("visibility_poor") else 10000,
                "alternatives_considered": [],
            }
            return facts

        agent_mod.build_facts = build
        return restore

    # FAIL-GEOCODE and anything else runs the real path.
    return restore


_NUM = re.compile(r"\d+(?:\.\d+)?")


def check_grounded(answer: str, facts: dict, citations: list[str]) -> tuple[bool, str]:
    """Independent re-check of the verify node's rule.

    Allowed numbers come from two auditable sources: values the API returned
    for THIS request, and numbers written into the cited policy text itself
    (thresholds like 40, durations like "15-20 minutes").
    """
    allowed: set[str] = set()
    for v in weather_mod.numeric_facts(facts).values():
        allowed.update({f"{v:g}", f"{round(v):g}", f"{v:.1f}"})

    from app.policy import load_policy_book

    book = load_policy_book()
    for cite in citations:
        sop = book.by_id(cite.split(" - ")[0])
        if sop:
            allowed.update(_NUM.findall(sop.guidance + " " + sop.when))

    # Clock times are phrasing, not claimed measurements.
    scrubbed = re.sub(r"\b\d{1,2}[:.]\d{2}\b", " ", answer)
    bad = [n for n in set(_NUM.findall(scrubbed)) if n not in allowed]
    return (not bad), ("ungrounded numbers: " + ", ".join(sorted(bad)) if bad else "")


def run_case(case: dict) -> dict:
    restore = install_stubs(case)
    try:
        question = " ".join(case["question"].split())
        if case.get("location") and case["location"].lower() not in question.lower():
            question = f"{question} (in {case['location']})"
        out = agent_mod.ask(question, thread_id=f"eval-{case['id']}-{uuid.uuid4().hex[:6]}")
    except Exception as exc:
        restore()
        return {
            "id": case["id"],
            "passed": False,
            "notes": [f"raised: {exc}"],
            "answer": "",
            "citations": [],
            "why": case.get("why", ""),
        }
    finally:
        restore()

    exp = case.get("expect", {})
    answer = out["answer"]
    cites = out["citations"]
    cited_ids = [c.split(" - ")[0] for c in cites]
    notes: list[str] = []
    ok = True

    if "cites_any" in exp:
        if not any(i in cited_ids for i in exp["cites_any"]):
            ok = False
            notes.append(f"expected one of {exp['cites_any']}, cited {cited_ids or 'nothing'}")

    if exp.get("no_citation") and cites:
        ok = False
        notes.append(f"expected no citation, got {cited_ids}")

    if "contains_any" in exp:
        if not any(s.lower() in answer.lower() for s in exp["contains_any"]):
            ok = False
            notes.append(f"missing any of {exp['contains_any']}")

    if "not_contains" in exp:
        hits = [s for s in exp["not_contains"] if s.lower() in answer.lower()]
        if hits:
            ok = False
            notes.append(f"contains forbidden {hits}")

    if "error_kind" in exp:
        if out.get("error_kind") != exp["error_kind"]:
            ok = False
            notes.append(f"expected error_kind={exp['error_kind']}, got {out.get('error_kind')}")

    if exp.get("grounded"):
        # Check against the same fact set the verify node used. ask() strips
        # keys starting with "_", so _meta (which holds visibility_m) has to be
        # put back or legitimate numbers look ungrounded to the suite.
        facts_for_check = {**out.get("facts", {}), "_meta": out.get("meta", {})}
        good, msg = check_grounded(answer, facts_for_check, cites)
        if not good:
            ok = False
            notes.append(msg)

    return {
        "id": case["id"],
        "passed": ok,
        "notes": notes,
        "answer": answer,
        "citations": cited_ids,
        "why": case.get("why", ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="include cases marked live")
    ap.add_argument("--only", help="run a single case id")
    args = ap.parse_args()

    with open(CASES, encoding="utf-8") as fh:
        cases = yaml.safe_load(fh)["cases"]

    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    elif not args.live:
        cases = [c for c in cases if not c.get("live")]

    results = []
    for case in cases:
        print(f"\n--- {case['id']} " + "-" * max(4, 58 - len(case["id"])))
        print(f"checking: {' '.join(case.get('why', '').split())}")
        r = run_case(case)
        results.append(r)
        print(f"question: {' '.join(case['question'].split())[:100]}")
        print(f"answer:   {r['answer'][:220]}")
        print(f"cited:    {r['citations'] or '(none)'}")
        print(f"result:   {'PASS' if r['passed'] else 'FAIL'}")
        for n in r["notes"]:
            print(f"          ! {n}")

    passed = sum(1 for r in results if r["passed"])
    print("\n" + "=" * 64)
    print(f"{passed}/{len(results)} passed")
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['id']}: {'; '.join(r['notes'])}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
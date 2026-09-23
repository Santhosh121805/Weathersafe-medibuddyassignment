

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from app import prompts
from app.llm import get_llm
from app.policy import (
    Match,
    candidate_fuzzy_sops,
    load_policy_book,
    match_deterministic,
    rank_and_split,
)
from app.weather import (
    WeatherError,
    build_facts,
    describe_facts,
    fetch_forecast,
    numeric_facts,
    resolve_location,
)



class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    question: str
    activity: str
    city: str | None
    last_location: str | None      # carried across turns
    facts: dict[str, Any]
    primary: dict[str, Any] | None
    secondary: list[dict[str, Any]]
    answer: str
    error_kind: str | None
    citations: list[str]
    trace: list[str]
    verify_attempts: int




class Intent(BaseModel):
    activity: str = Field(description="closest activity from the allowed list")
    location: str | None = Field(default=None, description="place name, or null")
    is_followup: bool = False


class FuzzyVerdict(BaseModel):
    applies: bool
    reason: str


def _history(messages: list[BaseMessage], limit: int = 6) -> str:
    if not messages:
        return "(none)"
    recent = messages[-limit:]
    out = []
    for m in recent:
        role = "User" if isinstance(m, HumanMessage) else "Assistant"
        out.append(f"{role}: {m.content}")
    return "\n".join(out)



def parse_intent(state: AgentState) -> dict:
    book = load_policy_book()
    llm = get_llm(temperature=0).with_structured_output(Intent)

    system = prompts.INTENT_SYSTEM.format(
        activities=", ".join(book.activities),
        history=_history(state.get("messages", [])[:-1]),
        last_location=state.get("last_location") or "(none)",
    )
    try:
        intent = llm.invoke(
            [("system", system), ("human", state["question"])]
        )
        activity = intent.activity if intent.activity in book.activities else "unknown"
        city = intent.location or state.get("last_location")
    except Exception as exc:  # a classifier failure must not fake an answer
        import traceback
        traceback.print_exc()
        cause = getattr(exc, "__cause__", None)
        return {
            "activity": "unknown",
            "city": state.get("last_location"),
            "trace": state.get("trace", []) + [
                f"parse_intent FAILED {type(exc).__name__}: {exc} || cause "
                f"{type(cause).__name__}: {cause}"
            ],
        }


def resolve_location_node(state: AgentState) -> dict:
    city = state.get("city")
    if not city:
        return {"error_kind": "no_location"}
    try:
        loc = resolve_location(city)
    except WeatherError as exc:
        return {
            "error_kind": exc.kind,
            "trace": state.get("trace", []) + [f"geocode failed: {exc.kind}"],
        }
    return {
        "city": loc.label,
        "last_location": loc.label,
        "facts": {"_location_obj": loc},
        "trace": state.get("trace", []) + [f"resolved: {loc.label}"],
    }


def fetch_weather(state: AgentState) -> dict:
    loc = state["facts"]["_location_obj"]
    try:
        data = fetch_forecast(loc)
        facts = build_facts(data, loc)
    except WeatherError as exc:
        return {
            "error_kind": exc.kind,
            "trace": state.get("trace", []) + [f"forecast failed: {exc.kind}"],
        }
    return {
        "facts": facts,
        "trace": state.get("trace", []) + ["forecast fetched"],
    }


def match_policy(state: AgentState) -> dict:
    book = load_policy_book()
    facts = state["facts"]
    activity = state.get("activity", "unknown")

    matches: list[Match] = match_deterministic(book, facts, activity)
    trace = state.get("trace", []) + [
        f"deterministic matches: {[m.sop.id for m in matches] or 'none'}"
    ]

    
    if not matches:
        for sop in candidate_fuzzy_sops(book, activity):
            try:
                llm = get_llm(temperature=0).with_structured_output(FuzzyVerdict)
                verdict = llm.invoke([
                    ("system", prompts.FUZZY_SYSTEM.format(
                        sop_id=sop.id,
                        when=sop.when,
                        facts=describe_facts(facts),
                        question=state["question"],
                        activity=activity,
                    )),
                    ("human", "Does this rule apply?"),
                ])
                if verdict.applies:
                    matches.append(Match(sop=sop, reason=verdict.reason, kind="fuzzy"))
                trace.append(f"fuzzy {sop.id}: {verdict.applies} ({verdict.reason})")
            except Exception as exc:
                trace.append(f"fuzzy {sop.id} judge failed: {exc}")

    primary, secondary = rank_and_split(book, matches)
    return {
        "primary": primary.to_dict() if primary else None,
        "secondary": [m.to_dict() for m in secondary],
        "citations": (
            [primary.sop.citation()] + [m.sop.citation() for m in secondary]
            if primary else []
        ),
        "trace": trace + [f"primary: {primary.sop.id if primary else 'none'}"],
    }


def compose(state: AgentState) -> dict:
    primary = state["primary"]
    secondary = state.get("secondary", [])

    secondary_block = "\n\n".join(
        prompts.SECONDARY_TEMPLATE.format(
            sop_id=s["id"], severity=s["severity"], title=s["title"],
            guidance=s["guidance"],
        )
        for s in secondary
    ) or "(no additional policies apply)"

    system = prompts.COMPOSE_SYSTEM.format(
        facts=describe_facts(state["facts"]),
        primary_id=primary["id"],
        primary_severity=primary["severity"],
        primary_title=primary["title"],
        primary_guidance=primary["guidance"],
        secondary_block=secondary_block,
        history=_history(state.get("messages", [])[:-1]),
    )
    extra = ""
    if state.get("verify_attempts", 0) > 0:
        extra = (
            "\n\nYour previous attempt stated a number that was not in the "
            "WEATHER FACTS block. Rewrite using only numbers copied from it."
        )

    llm = get_llm(temperature=0.2)
    reply = llm.invoke([("system", system + extra), ("human", state["question"])])
    return {"answer": reply.content.strip()}



_NUM = re.compile(r"\d+(?:\.\d+)?")


def _allowed_numbers(state: AgentState) -> set[str]:
    """Numbers the answer may contain.

    Two sources, both auditable: values the API returned for THIS request, and
    numbers written into the policy text itself (thresholds like 40, durations
    like "15-20 minutes", the 30-minute lightning rule).
    """
    allowed: set[str] = set()

    for v in numeric_facts(state["facts"]).values():
        allowed.add(f"{v:g}")
        allowed.add(f"{round(v):g}")
        allowed.add(f"{v:.1f}")

    policy_text = " ".join(
        [state["primary"]["guidance"], state["primary"]["when"]]
        + [s["guidance"] for s in state.get("secondary", [])]
        + [s["when"] for s in state.get("secondary", [])]
    )
    allowed.update(_NUM.findall(policy_text))
    return allowed


def verify(state: AgentState) -> dict:
    answer = state["answer"]
    allowed = _allowed_numbers(state)

    
    scrubbed = re.sub(r"\b\d{1,2}[:.]\d{2}\b", " ", answer)
    stated = set(_NUM.findall(scrubbed))
    ungrounded = {n for n in stated if n not in allowed}

    trace = state.get("trace", [])
    if not ungrounded:
        return {"trace": trace + ["verify: ok"]}

    attempts = state.get("verify_attempts", 0)
    if attempts == 0:
        return {
            "verify_attempts": 1,
            "trace": trace + [f"verify: ungrounded {sorted(ungrounded)}, retrying"],
        }

    
    p = state["primary"]
    loc = state["facts"].get("_meta", {}).get("location", "your location")
    fallback = (
        f"For {loc}, our guidance {p['id']} ({p['title']}) applies. {p['guidance']} "
        f"I've kept this to the written policy because I couldn't confirm every "
        f"figure in my draft against the forecast I retrieved."
    )
    return {
        "answer": fallback,
        "verify_attempts": 2,
        "trace": trace + [f"verify: ungrounded {sorted(ungrounded)}, used fallback"],
    }




def no_match(state: AgentState) -> dict:
    activity = state.get("activity", "unknown")
    phrase = "that" if activity == "unknown" else activity.replace("_", " ")
    loc = state["facts"].get("_meta", {}).get("location", state.get("city", "that area"))
    return {
        "answer": prompts.NO_MATCH_TEMPLATE.format(location=loc, activity_phrase=phrase),
        "citations": [],
        "trace": state.get("trace", []) + ["no policy matched"],
    }


def honest_failure(state: AgentState) -> dict:
    kind = state.get("error_kind", "forecast_failed")
    template = prompts.FAILURE_TEMPLATES.get(kind, prompts.FAILURE_TEMPLATES["forecast_failed"])
    return {
        "answer": template.format(city=state.get("city") or "that location"),
        "citations": [],
        "trace": state.get("trace", []) + [f"honest failure: {kind}"],
    }




def after_resolve(state: AgentState) -> Literal["fetch_weather", "honest_failure"]:
    return "honest_failure" if state.get("error_kind") else "fetch_weather"


def after_fetch(state: AgentState) -> Literal["match_policy", "honest_failure"]:
    return "honest_failure" if state.get("error_kind") else "match_policy"


def after_match(state: AgentState) -> Literal["compose", "no_match"]:
    return "compose" if state.get("primary") else "no_match"


def after_verify(state: AgentState) -> Literal["compose", "finish"]:
    if state.get("verify_attempts") == 1 and "retrying" in (state.get("trace") or [""])[-1]:
        return "compose"
    return "finish"


def finish(state: AgentState) -> dict:
    return {"messages": [AIMessage(content=state["answer"])]}




def build_graph():
    g = StateGraph(AgentState)

    g.add_node("parse_intent", parse_intent)
    g.add_node("resolve_location", resolve_location_node)
    g.add_node("fetch_weather", fetch_weather)
    g.add_node("match_policy", match_policy)
    g.add_node("compose", compose)
    g.add_node("verify", verify)
    g.add_node("no_match", no_match)
    g.add_node("honest_failure", honest_failure)
    g.add_node("finish", finish)

    g.add_edge(START, "parse_intent")
    g.add_edge("parse_intent", "resolve_location")
    g.add_conditional_edges("resolve_location", after_resolve)
    g.add_conditional_edges("fetch_weather", after_fetch)
    g.add_conditional_edges("match_policy", after_match)
    g.add_edge("compose", "verify")
    g.add_conditional_edges("verify", after_verify)
    g.add_edge("no_match", "finish")
    g.add_edge("honest_failure", "finish")
    g.add_edge("finish", END)

    return g.compile(checkpointer=MemorySaver())


GRAPH = build_graph()


def ask(question: str, thread_id: str = "default") -> dict:
    """Run one turn. Memory is per thread_id and lives only in this process."""
    result = GRAPH.invoke(
        {"question": question, "messages": [HumanMessage(content=question)]},
        config={"configurable": {"thread_id": thread_id}},
    )
    return {
        "answer": result.get("answer", ""),
        "citations": result.get("citations", []),
         "severity": (result.get("primary") or {}).get("severity"),
        "policy_id": (result.get("primary") or {}).get("id"),
        "activity": result.get("activity"),
        "location": result.get("city"),
        "error_kind": result.get("error_kind"),
        "trace": result.get("trace", []),
        "facts": {
            k: v for k, v in (result.get("facts") or {}).items()
            if not k.startswith("_")
        },
       
        "meta": (result.get("facts") or {}).get("_meta", {}),
    }
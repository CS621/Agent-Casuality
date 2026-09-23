"""This creates .casuality/events.db and records the causal event chain."""

import casuality

casuality.init()

@casuality.agent(role="researcher")
def research(query):
    return {"answer": query}

@casuality.merge_decision(ports=["research_summary"])
def approve(research_summary):
    return "approved" if research_summary["answer"] == "yes" else "rejected"

print(approve(research("yes")))
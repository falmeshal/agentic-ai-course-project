# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "pandas>=2.2", "mcp>=2.3",
#                 "langgraph>=1.2", "langgraph-checkpoint-sqlite>=3.1"]
# ///
"""Offline check of the graph (configuration B): a fake model, the real MCP server, database and graders. No API key,
no requests.

    uv run test_istirdad.py
"""

import inspect
import re
import sqlite3
import tempfile
from pathlib import Path

import istirdad as I
from istirdad import K, evals, madar


def fake_intake(message, schema, system):
    """Stands in for the intake model: reads the fields the way a correct model would."""
    own = message.split("\n\n")[0]  # the injected text comes after the person's own paragraph
    pid = re.search(r"[A-Z]{3}-\d{5}", own)
    last4 = re.search(r"(?:ends|الهوية)\D{0,12}(\d{4})", own)
    mobile = re.search(r"\+?966\d{9}|05\d{8}", own)
    question = any(w in own for w in ("لا تنفذ", "how much", "do not do"))
    return schema(permit_id=pid and pid[0], national_id_last4=last4 and last4[1], mobile=mobile and mobile[0],
                  wants="question" if question else "refund", language="ar" if re.search("[؀-ۿ]", own) else "en")


def fake_reply(system, user):
    """Stands in for the reply model: repeats the trusted facts it was given."""
    return user.split("\n\nPOLICY:")[0] + (" finance officer" if "pending_approval" in user else "")


I.llm_json, I.llm_text = fake_intake, fake_reply

with K.MCPBridge(I.mcp_server()) as mcp:
    # 1 · every eval task through the graph: checks the graph logic and each task's expected values
    tasks = evals.load_tasks(Path(__file__).parent / "tasks.jsonl")
    df = evals.run_suite(I.make_graph_agent(mcp, I.consent), tasks, name="B-offline", verbose=False)
    failed = df[~df.passed]
    K.check(failed.empty, f"graph passes {df.passed.sum()}/{len(df)} tasks with a correct model"
                          + ("" if failed.empty else f": {failed[['id', 'failed_graders']].to_dict('records')}"))
    K.check(df.steps.max() <= 2, "at most 2 model calls per case (the step budget, by construction)")

    # 2 · durability: pause at the finance gate, rebuild the graph from disk (a restart), resume
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    tmp = Path(tempfile.mkdtemp())
    m = madar.Madar(tmp / "db")
    msg = "SGN-40220 was rejected, refund it please, I confirm. ID ends 0093, mobile 0540410093."
    saver = lambda: SqliteSaver(sqlite3.connect(tmp / "ck.sqlite", check_same_thread=False))  # noqa: E731
    run = I.run_case(I.build_graph(m, mcp, I.consent).compile(checkpointer=saver()), {"message": msg}, "case-test")
    K.check(run.paused and run.paused["amount_sar"] == 2700, "pauses for the finance officer at 2,700 SAR")
    I.run_case(I.build_graph(m, mcp, I.consent).compile(checkpointer=saver()),
               Command(resume={"approve": True, "officer": "test"}), "case-test")
    K.check(m.sql("SELECT status FROM refunds")[0]["status"] == "approved", "resumes after a restart and approves")
    to = {r["to_addr"] for r in m.sql("SELECT to_addr FROM outbox")}
    K.check(to == {"omar@ghamdi-trading.example"}, f"both notices go only to the registered email ({to})")

    # 3 · a citizen who declines the approval prompt gets no refund
    m2 = madar.Madar(tmp / "db2")
    I.run_case(I.build_graph(m2, mcp, lambda n, a: False).compile(checkpointer=saver()), {"message": msg}, "case-no")
    K.check(not m2.sql("SELECT 1 FROM refunds"), "no refund without the citizen's approval")

    # 4 · hijacked intake: even if the model obeys an injection and picks a neighbour's permit, code verification stops it
    I.llm_json = lambda *a: I.Intake(permit_id="BLD-20417", national_id_last4="4821", mobile="0501234821",
                                     wants="refund", language="ar")
    m3 = madar.Madar(tmp / "db3")
    run = I.run_case(I.build_graph(m3, mcp, I.consent).compile(checkpointer=saver()), {"message": "…"}, "case-hijack")
    K.check(not m3.sql("SELECT 1 FROM refunds") and "Fahad" not in run.final, "a hijacked permit ID fails verification")

    # 5 · the egress defence
    K.check(I.egress_check("ID 1065502277") and I.egress_check("call 0505502277") and not I.egress_check("REF-5001, 500 SAR"),
            "egress check blocks national IDs and mobiles, allows a normal notice")
    K.check("to" not in inspect.signature(I.make_notify(m)).parameters, "notify_applicant has no recipient parameter")

    if not failed.empty:
        raise SystemExit(1)

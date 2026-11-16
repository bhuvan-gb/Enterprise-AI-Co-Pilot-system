"""Gemini integration (plain REST, no SDK needed) plus clearly-labelled demo fallbacks.

Every public function returns a dict containing "mode": "live" (real Gemini answer) or
"demo" (no API key -> rule-based / extractive result; the UI labels it as such).
"""
import json
import re
import socket
import urllib.error
import urllib.request
from datetime import date, timedelta

from flask import current_app

from models import Task
from services import rag_service
from services.text_utils import extractive_points, extractive_summary, top_keywords

DEMO_NOTICE = ("**Demo mode — Gemini API key not configured.** Live AI generation is switched off, so this is "
               "not an AI-written answer. Add `GEMINI_API_KEY` to your `.env` file and restart the app to enable it.")


class AIError(Exception):
    """Raised with a user-readable message when the Gemini call fails."""


def is_configured():
    key = (current_app.config.get("GEMINI_API_KEY") or "").strip()
    return bool(key) and not key.lower().startswith(("your", "paste", "changeme", "xxx"))


# ------------------------------------------------------------------ low level
def _call(contents, system=None, temperature=0.4, json_mode=False, max_tokens=4096):
    key = current_app.config["GEMINI_API_KEY"].strip()
    model = current_app.config["GEMINI_MODEL"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": contents, "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=current_app.config["AI_TIMEOUT"]) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        if exc.code in (400, 401, 403) and "key" in detail.lower():
            raise AIError("Gemini rejected the API key. Check GEMINI_API_KEY in your .env file.")
        if exc.code == 404:
            raise AIError(f"Gemini model “{model}” was not found. Check GEMINI_MODEL in your .env file.")
        if exc.code == 429:
            raise AIError("Gemini rate limit or quota reached. Wait a moment and try again.")
        raise AIError(f"Gemini returned an error ({exc.code}). {detail[:200]}")
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        raise AIError("Could not reach the Gemini API (network error or timeout). Check your connection.")
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise AIError(f"Gemini blocked this request ({reason})." if reason else "Gemini returned no answer.")
    text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", [])).strip()
    if not text:
        raise AIError("Gemini returned an empty response. Try rephrasing.")
    return text


def _normalize(history, prompt):
    """Gemini needs alternating turns that start with the user. Merge/trim history accordingly."""
    turns = []
    for m in list(history or []) + [{"role": "user", "content": prompt}]:
        role = "user" if m["role"] == "user" else "model"
        if turns and turns[-1]["role"] == role:
            turns[-1]["parts"][0]["text"] += "\n\n" + m["content"]
        else:
            turns.append({"role": role, "parts": [{"text": m["content"]}]})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def generate(prompt, system=None, history=None, temperature=0.4, json_mode=False):
    return _call(_normalize(history, prompt), system, temperature, json_mode)


def _parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except ValueError:
                pass
        s, e = text.find("["), text.rfind("]")
        if s != -1 and e > s:
            return json.loads(text[s:e + 1])
        raise AIError("Gemini returned an answer in an unexpected format. Please try again.")


# ------------------------------------------------------------------ chat + RAG
def _user_context(user):
    open_tasks = Task.query.filter(Task.assigned_to == user.id, Task.status != "Completed") \
        .order_by(Task.due_date).limit(8).all()
    lines = [f"Name: {user.full_name}; role: {user.role}; department: "
             f"{user.department.name if user.department else 'n/a'}; designation: {user.designation or 'n/a'}.",
             f"Today is {date.today():%A, %d %B %Y}."]
    if open_tasks:
        lines.append("Their open tasks: " + "; ".join(
            f"{t.title} ({t.priority}, due {t.due_date:%d %b} , {t.effective_status})" if t.due_date
            else f"{t.title} ({t.priority}, {t.effective_status})" for t in open_tasks))
    return "\n".join(lines)


SYSTEM = ("You are Enterprise AI Co-Pilot, a workplace assistant inside a company platform. Be concise, accurate and "
          "professional. When CONTEXT excerpts are provided, ground company-specific answers in them and cite sources "
          "as [1], [2]. If the context does not contain the answer, say so plainly instead of inventing company "
          "facts. You may answer general workplace questions from your own knowledge. Use Markdown.")


def answer(user, question, history=None, document_id=None):
    """RAG answer. Returns {"text","sources","mode"}. Raises AIError on live-call failure."""
    results = rag_service.search(user, question, document_id=document_id)
    sources = rag_service.source_list(results)
    if not is_configured():
        return {"text": _demo_answer(user, question, results), "sources": sources, "mode": "demo"}
    ctx = rag_service.build_context(results) or "(no relevant documents or knowledge-base entries were found)"
    prompt = f"CONTEXT:\n{ctx}\n\nUSER INFO:\n{_user_context(user)}\n\nQUESTION:\n{question}"
    text = generate(prompt, system=SYSTEM, history=(history or [])[-10:])
    return {"text": text, "sources": sources, "mode": "live"}


def _demo_answer(user, question, results):
    q = question.lower()
    parts = [DEMO_NOTICE, ""]
    if "task" in q or "to do" in q or "deadline" in q:
        tasks = Task.query.filter(Task.assigned_to == user.id, Task.status != "Completed") \
            .order_by(Task.due_date).limit(8).all()
        if tasks:
            parts.append("**Your open tasks (from the database):**")
            parts += [f"- {t.title} — {t.priority}, {t.effective_status}"
                      + (f", due {t.due_date:%d %b %Y}" if t.due_date else "") for t in tasks]
        else:
            parts.append("You have no open tasks assigned to you.")
        parts.append("")
    if results:
        parts.append("**Most relevant passages from documents and knowledge-base entries you can access:**")
        for i, r in enumerate(results[:3], 1):
            snippet = re.sub(r"\s+", " ", r["text"]).strip()
            parts.append(f"> [{i}] {snippet[:380]}{'…' if len(snippet) > 380 else ''}")
    elif "task" not in q:
        parts.append("No matching passages were found in your accessible documents or the knowledge base.")
    return "\n".join(parts)


# ------------------------------------------------------------------ documents
def summarize_document(doc, text):
    """Returns {"summary","key_points","keywords","mode"}."""
    if not is_configured():
        return {"summary": extractive_summary(text), "key_points": extractive_points(text),
                "keywords": top_keywords(text), "mode": "extractive"}
    prompt = ("Summarise this document. Respond ONLY with JSON: {\"summary\": \"3-5 sentence summary\", "
              "\"key_points\": [\"5-8 important points\"], \"keywords\": [\"8-12 keywords\"]}.\n\n"
              f"TITLE: {doc.title}\n\nTEXT:\n{text}")
    data = _parse_json(generate(prompt, temperature=0.2, json_mode=True))
    return {"summary": str(data.get("summary", "")).strip(),
            "key_points": [str(p) for p in data.get("key_points", [])][:10],
            "keywords": [str(k) for k in data.get("keywords", [])][:15], "mode": "gemini"}


# ------------------------------------------------------------------ tasks
_URGENT = ("urgent", "asap", "critical", "outage", "breach", "security", "incident", "production", "immediately")


def suggest_priority(title, description, due):
    if is_configured():
        prompt = ("Suggest a priority for this task. Respond ONLY with JSON "
                  "{\"priority\": \"Low|Medium|High|Critical\", \"reason\": \"one sentence\"}.\n"
                  f"Title: {title}\nDescription: {description}\nDue: {due or 'no deadline'}\nToday: {date.today()}")
        data = _parse_json(generate(prompt, temperature=0.1, json_mode=True))
        pr = str(data.get("priority", "Medium")).title()
        return {"priority": pr if pr in ("Low", "Medium", "High", "Critical") else "Medium",
                "reason": str(data.get("reason", "")), "mode": "live"}
    text = f"{title} {description}".lower()
    pr, why = "Medium", "Default priority."
    if any(w in text for w in _URGENT):
        pr, why = "High", "Contains urgency keywords."
    if due:
        days = (due - date.today()).days
        if days < 0:
            pr, why = "Critical", "The deadline has already passed."
        elif days <= 2:
            pr, why = "High", f"Due in {days} day(s)."
        elif days > 14 and pr == "Medium":
            pr, why = "Low", "Deadline is more than two weeks away."
    return {"priority": pr, "reason": why + " (rule-based demo suggestion, not AI)", "mode": "demo"}


def generate_description(title, category):
    if is_configured():
        text = generate(f"Write a clear 3-4 sentence task description with a definition of done for this task.\n"
                        f"Title: {title}\nCategory: {category}\nReturn plain text only.", temperature=0.5)
        return {"description": text, "mode": "live"}
    return {"description": f"{title}.\n\nScope: complete the work described by the title for the {category} team.\n"
                           f"Definition of done: deliverable reviewed and accepted by the requester.\n"
                           f"(Template text — enable Gemini for an AI-written description.)", "mode": "demo"}


def suggest_subtasks(title, description):
    if is_configured():
        data = _parse_json(generate(
            "Break this task into 4-7 concrete, ordered subtasks. Respond ONLY with JSON "
            f"{{\"subtasks\": [\"...\"]}}.\nTitle: {title}\nDescription: {description}", temperature=0.3, json_mode=True))
        items = [str(s).strip()[:190] for s in data.get("subtasks", []) if str(s).strip()]
        return {"subtasks": items[:8], "mode": "live"}
    return {"subtasks": [f"Clarify scope and success criteria for: {title}"[:190], "Break the work into steps",
                         "Complete the main work", "Review and test the result", "Share the outcome with stakeholders"],
            "mode": "demo"}


def _task_lines(tasks):
    return "\n".join(f"- {t.title} | {t.priority} | {t.effective_status} | "
                     f"{('due ' + t.due_date.isoformat()) if t.due_date else 'no deadline'} | "
                     f"{t.assignee.full_name if t.assignee else 'unassigned'}" for t in tasks[:60])


def summarize_tasks(tasks):
    if not tasks:
        return {"text": "There are no pending tasks to summarise.", "mode": "live" if is_configured() else "demo"}
    if is_configured():
        return {"text": generate("Summarise these pending tasks for a busy employee: what is most urgent, what is "
                                 "overdue and a suggested order of work. Use short Markdown bullets.\n"
                                 + _task_lines(tasks)), "mode": "live"}
    overdue = [t for t in tasks if t.effective_status == "Overdue"]
    high = [t for t in tasks if t.priority in ("High", "Critical")]
    return {"text": f"{DEMO_NOTICE}\n\n**Counts from the database:** {len(tasks)} pending, {len(overdue)} overdue, "
                    f"{len(high)} high/critical priority.", "mode": "demo"}


def workload_suggestions(rows):
    """rows: list of dicts {name, open, overdue, high}."""
    table = "\n".join(f"- {r['name']}: {r['open']} open, {r['overdue']} overdue, {r['high']} high priority" for r in rows)
    if is_configured():
        return {"text": generate("You are an operations analyst. Based on this workload, suggest concrete ways to "
                                 "rebalance work and reduce overdue items. Short Markdown bullets.\n" + table),
                "mode": "live"}
    if not rows:
        return {"text": DEMO_NOTICE + "\n\nNo open tasks to analyse.", "mode": "demo"}
    heavy = max(rows, key=lambda r: r["open"])
    light = min(rows, key=lambda r: r["open"])
    return {"text": f"{DEMO_NOTICE}\n\n**Rule-based observation:** {heavy['name']} has the most open tasks "
                    f"({heavy['open']}); {light['name']} has the fewest ({light['open']}). Consider reassigning.",
            "mode": "demo"}


# ------------------------------------------------------------------ analytics / reports
def business_summary(stats_text):
    if is_configured():
        return {"text": generate("You are a business analyst. Write an executive summary (max 180 words) of this "
                                 "organisation data: key findings, risks and 3 recommendations. Use only the figures "
                                 "given; do not invent numbers.\n\n" + stats_text, temperature=0.3), "mode": "live"}
    return {"text": f"{DEMO_NOTICE}\n\n**Figures from your database:**\n\n{stats_text}", "mode": "demo"}


def report_summary(report_title, stats_text):
    if is_configured():
        return {"text": generate(f"Write a 4-6 sentence executive summary for the report “{report_title}”. Use only "
                                 f"these figures; do not invent numbers.\n\n{stats_text}", temperature=0.3),
                "mode": "live"}
    return {"text": "Automatic summary (demo mode — not AI-generated): " + stats_text.replace("\n", "; "),
            "mode": "demo"}

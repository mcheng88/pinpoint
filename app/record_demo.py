"""Record what the two screens need from a running copy of the app, so they can be shown with no server.

    python app/record_demo.py http://localhost:8000 demo/data.json [user:password]

Asks a fixed list of questions in a new case, and saves each reply with its summary, its Spanish
version and the source text of every passage it names. Also saves the Sources screen's data and a
few keyword searches. The case made for the recording is deleted at the end.
"""
import base64
import json
import sys
import time
import urllib.parse
import urllib.request

FACTS = {"plan": "Managed long-term care", "what": "Disenrolled from the plan", "service": "Home care",
         "notice": "No notice", "still": "Yes"}
ROSA = ("Rosa Delgado, born March 3, 1948, Medicaid ID AB12345C, lives at 245 East 116th Street, phone 212-555-0147. "
        "Her plan disenrolled her last month. What are the reasons that a Medicaid plan may disenroll a member?")
# The questions the Cases screen offers as buttons, each asked with the "what happened" it is offered under.
OFFERED = {
    "Disenrolled from the plan": [ROSA, "Can a plan disenroll someone because she needs more care?",
                                  "What are the reasons that a Medicaid plan may disenroll a member?",
                                  "What process is supposed to be followed for these disenrollments?",
                                  "Which entity was responsible for not following the correct process?",
                                  "What appeal or complaint procedure is appropriate to rectify the error?",
                                  "Should she sue her plan?"],
    "Service reduced or stopped": ["Can services continue during an appeal?"],
    "Service denied": ["What must a notice of adverse benefit determination include?", "How long does an enrollee have to file an appeal?"],
    "Prior authorization denied": ["Can an enrollee request an expedited appeal?"],
    "Cannot find a provider": ["What must a plan do when it cannot provide a service in network?", "What are the network adequacy standards?"],
    "Something else": ["What are the enrollee rights under managed care?"],
}
QUESTIONS = [(q, {**FACTS, "what": what}) for what, qs in OFFERED.items() for q in qs]
FIND = ["payor of last resort", "involuntary disenrollment", "fair hearing", "aid continuing", "person-centered service plan"]


def main(base, out, auth=None):
    head = {"Content-Type": "application/json", "User-Agent": "pinpoint-record-demo"}
    if auth:
        head["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()

    def call(path, body=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, head)
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())

    def passage_ids(x, found):
        """Every passage a reply names: a dict that has both an id and a citation."""
        if isinstance(x, dict):
            if isinstance(x.get("id"), int) and "cite" in x:
                found.add(x["id"])
            for v in x.values():
                passage_ids(v, found)
        elif isinstance(x, list):
            for v in x:
                passage_ids(v, found)
        return found

    data = {"recorded": time.strftime("%Y-%m-%d"), "facts": FACTS, "asks": [], "sections": {}, "find": {}}
    cid = call("/api/case", {"label": "Recording", "facts": FACTS})["id"]
    try:
        for q, facts in QUESTIONS:
            t = time.time()
            r = call("/api/ask", {"question": q, "plan": facts["plan"], "facts": facts, "case_id": cid})
            if r.get("writing"):
                r.update(call("/api/answer", {"ask_id": r["ask_id"], "question": r["sent"], "pool": r["pool"],
                                              "facts": facts, "strangers": r.get("strangers")}))
            spanish = None
            if r.get("summary"):
                spanish = call("/api/spanish", {"sentences": [s["text"] for s in r["summary"]]})
            data["asks"].append({"question": q, "facts": facts, "seconds": round(time.time() - t, 1), "reply": r, "spanish": spanish})
            print(f"{time.time() - t:5.1f} s  {r.get('kind')}  {q[:60]}", flush=True)
        for pid in sorted(passage_ids(data["asks"], set())):
            try:
                data["sections"][str(pid)] = call(f"/api/section?id={pid}")
            except Exception as e:
                print("no section", pid, e)
        for words in FIND:
            data["find"][words] = call("/api/find?q=" + urllib.parse.quote(words))
        data["sources"] = call("/api/sources")
        data["knowledge"] = call("/api/knowledge")
    finally:
        call("/api/case/delete", {"id": cid})
    with open(out, "w") as f:
        json.dump(data, f)
    print(f"{len(data['asks'])} questions, {len(data['sections'])} sections, {len(data['find'])} searches -> {out}")


if __name__ == "__main__":
    main(sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)

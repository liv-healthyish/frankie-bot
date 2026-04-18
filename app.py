import os
import hmac
import hashlib
import time
import json
import threading
import requests
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import anthropic

app = Flask(__name__)

# ── Clients ──────────────────────────────────────────────────────────────────
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
MONDAY_API_TOKEN     = os.environ["MONDAY_API_TOKEN"]
MONDAY_BOARD_ID      = "18387683486"
FRANKIE_CHANNEL      = "C0ATM8717GT"
BOT_USER_ID          = None

processed_events = set()


def verify_slack(req):
    ts  = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")
    if abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:{req.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def monday(query, variables=None):
    r = requests.post(
        "https://api.monday.com/v2",
        headers={"Authorization": MONDAY_API_TOKEN, "Content-Type": "application/json"},
        json={"query": query, **({"variables": variables} if variables else {})},
        timeout=15,
    )
    return r.json()


def get_active_tasks():
    q = """
    query ($boardId: ID!) {
      boards(ids: [$boardId]) {
        items_page(limit: 200, query_params: {rules: [{column_id: "status", compare_value: ["Done"], operator: not_any_of}]}) {
          items {
            id name
            column_values {
              id text
              ... on StatusValue { label }
              ... on DateValue    { date }
            }
          }
        }
      }
    }"""
    data = monday(q, {"boardId": MONDAY_BOARD_ID})
    items = data.get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
    tasks = []
    for item in items:
        cols = {c["id"]: c.get("text") or c.get("label") for c in item["column_values"]}
        tasks.append({
            "id":       item["id"],
            "name":     item["name"],
            "venture":  cols.get("color_mky2s354"),
            "priority": cols.get("color_mkyas1ez"),
            "type":     cols.get("color_mkxx8g5f"),
            "status":   cols.get("status"),
            "due_date": cols.get("date4"),
            "hours":    cols.get("numeric_mm2herm4"),
        })
    return tasks


def create_task(name, venture, priority, task_type, hours=None, due_date=None):
    VENTURE_IDS  = {"Healthyish Content": 1, "5HT": 0, "Fixie Dust": 2, "Healthyish Ventures": 3}
    PRIORITY_IDS = {"High": 1, "Medium": 2, "Low": 0}
    TYPE_IDS     = {"Heads-down": 0, "Moderate": 1, "Quick": 2, "Ongoing": 3}

    col_vals = {}
    if venture  in VENTURE_IDS:  col_vals["color_mky2s354"]  = {"label": venture}
    if priority in PRIORITY_IDS: col_vals["color_mkyas1ez"]  = {"label": priority}
    if task_type in TYPE_IDS:    col_vals["color_mkxx8g5f"]  = {"label": task_type}
    if hours:                    col_vals["numeric_mm2herm4"] = str(hours)
    if due_date:                 col_vals["date4"]            = {"date": due_date}

    q = """
    mutation ($boardId: ID!, $name: String!, $cols: JSON!) {
      create_item(board_id: $boardId, item_name: $name, column_values: $cols) { id name }
    }"""
    result = monday(q, {"boardId": MONDAY_BOARD_ID, "name": name, "cols": json.dumps(col_vals)})
    return result.get("data", {}).get("create_item", {})


def update_task_status(task_id, status):
    col_vals = {"status": {"label": status}}
    q = """
    mutation ($itemId: ID!, $boardId: ID!, $cols: JSON!) {
      change_multiple_column_values(item_id: $itemId, board_id: $boardId, column_values: $cols) { id }
    }"""
    monday(q, {"itemId": str(task_id), "boardId": MONDAY_BOARD_ID, "cols": json.dumps(col_vals)})
    return {"updated": True, "task_id": task_id, "new_status": status}


def search_tasks(query_text):
    tasks = get_active_tasks()
    q = query_text.lower()
    return [t for t in tasks if q in t["name"].lower()]


TOOLS = [
    {
        "name": "get_active_tasks",
        "description": "Get all active (non-Done) tasks from Liv's Monday.com board.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_tasks",
        "description": "Search active tasks by name keyword.",
        "input_schema": {
            "type": "object",
            "properties": {"query_text": {"type": "string"}},
            "required": ["query_text"],
        },
    },
    {
        "name": "create_task",
        "description": "Add a new task to Liv's Monday.com board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":      {"type": "string"},
                "venture":   {"type": "string", "enum": ["Healthyish Content", "5HT", "Fixie Dust", "Healthyish Ventures"]},
                "priority":  {"type": "string", "enum": ["High", "Medium", "Low"]},
                "task_type": {"type": "string", "enum": ["Heads-down", "Moderate", "Quick", "Ongoing"]},
                "hours":     {"type": "number"},
                "due_date":  {"type": "string", "description": "YYYY-MM-DD format"},
            },
            "required": ["name", "venture", "priority", "task_type"],
        },
    },
    {
        "name": "update_task_status",
        "description": "Update the status of a task on Monday.com.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status":  {"type": "string", "enum": ["Working on it", "Done", "Stuck", "Waiting", "Not started"]},
            },
            "required": ["task_id", "status"],
        },
    },
]

TOOL_HANDLERS = {
    "get_active_tasks":   lambda i: get_active_tasks(),
    "search_tasks":       lambda i: search_tasks(i["query_text"]),
    "create_task":        lambda i: create_task(i["name"], i["venture"], i["priority"], i["task_type"], i.get("hours"), i.get("due_date")),
    "update_task_status": lambda i: update_task_status(i["task_id"], i["status"]),
}

FRANKIE_SYSTEM = """You are Frankie — Liv Nicholl's trusted executive assistant and closest work friend. You are not a bot. You're sharp, organised, and genuinely invested in Liv's success.

WHO YOU'RE WORKING FOR
Liv Nicholl, COO of Healthyish Ventures. Slack: U0783NKAJP8. Works 8:30am–4:30pm ET Mon–Fri.

THE FOUR VENTURES
- Healthyish Content — core agency (content/SEO for health clients)
- 5HT — newsletter (growth, deliverability, ad revenue)
- Fixie Dust — CPG electrolyte powder launch, targeting June 1
- Healthyish Ventures — parent entity (legal structure, entity restructure in progress)

LIV'S WEEKLY STRUCTURE
- 2 hours of REAL focus time per day — hard limit. Never plan more unless she says otherwise.
- Mornings = focus. Afternoons = meetings + comms.
- Monday = Misc / ad-hoc tasks only.
- Tuesday–Thursday = WBR / strategic work.
- Friday = flexible.
- Recurring tasks have "Notes for Frankie" — always read these before surfacing them.

TASK TYPES
Heads-down (2+ hrs deep focus) | Moderate (30–90 min) | Quick (≤30 min) | Ongoing (recurring/strategic)

WBR vs AD-HOC
WBR = quarterly-goal-linked (hiring, product launch, ops automation, revenue, legal). Protect this time.
Ad-hoc = reactive, one-off. Slot on Mondays or around WBR.

YOUR PERSONALITY
- Talk like a brilliant friend who knows her world cold — not a corporate assistant.
- Direct. Warm. Occasionally funny. Short is better.
- Push back when something doesn't add up. Say it once, kindly, then move on.
- Celebrate real wins. Call out real avoidance. Never lecture.

HOW TO HANDLE REQUESTS
— "What should I focus on?" → Pull tasks, apply day-of-week logic, respect 2-hr limit, separate WBR vs ad-hoc.
— "Add/create a task" → Create it in Monday with venture + priority + type + estimate, confirm back.
— "Update [task]" → Search by name, update, confirm.
— "How are my WBR goals?" → Pull tasks, organise by venture, flag on-track vs at-risk vs blocked.
— Anything else → Use your judgment. You know her world.

IMPORTANT: Always use the Monday tools to get real data — don't make up task names or statuses."""


def ask_frankie(user_message, thread_history=None):
    messages = []
    if thread_history:
        messages.extend(thread_history)
    messages.append({"role": "user", "content": user_message})

    while True:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=FRANKIE_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    try:
                        result = TOOL_HANDLERS[block.name](block.input)
                    except Exception as e:
                        result = {"error": str(e)}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return "Something went wrong — couldn't get a response. Try again!"


def get_thread_history(channel, thread_ts, bot_id):
    try:
        result = slack.conversations_replies(channel=channel, ts=thread_ts, limit=20)
        messages = []
        for msg in result["messages"][:-1]:
            role = "assistant" if msg.get("user") == bot_id else "user"
            text = msg.get("text", "").strip()
            if text:
                messages.append({"role": role, "content": text})
        return messages or None
    except Exception:
        return None


def handle_event(payload):
    global BOT_USER_ID
    event = payload.get("event", {})
    event_type = event.get("type")
    event_id = payload.get("event_id")

    if event_id in processed_events:
        return
    processed_events.add(event_id)
    if len(processed_events) > 500:
        processed_events.clear()

    if not BOT_USER_ID:
        try:
            BOT_USER_ID = slack.auth_test()["user_id"]
        except Exception:
            pass

    if event.get("user") == BOT_USER_ID:
        return

    channel = event.get("channel", "")
    text = event.get("text", "").strip()
    thread_ts = event.get("thread_ts") or event.get("ts")
    msg_ts = event.get("ts")

    if not text:
        return

    should_respond = False

    if event_type == "app_mention":
        # Tagged anywhere in Slack — always respond
        should_respond = True
        if BOT_USER_ID:
            text = text.replace(f"<@{BOT_USER_ID}>", "").strip()
    elif event_type == "message" and not event.get("subtype"):
        # DMs or #frankie-ea — always respond
        if channel.startswith("D") or channel == FRANKIE_CHANNEL:
            should_respond = True

    if not should_respond or not text:
        return

    history = None
    if event.get("thread_ts") and event.get("thread_ts") != msg_ts:
        history = get_thread_history(channel, thread_ts, BOT_USER_ID)

    reply = ask_frankie(text, history)

    try:
        slack.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)
    except SlackApiError as e:
        print(f"Slack error: {e}")


@app.route("/slack/events", methods=["POST"])
def slack_events():
    if not verify_slack(request):
        return jsonify({"error": "invalid signature"}), 403

    payload = request.json

    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload["challenge"]})

    threading.Thread(target=handle_event, args=(payload,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "Frankie is online ✨"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)

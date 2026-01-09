"""
MCP Server for health data.
Exposes health metrics to Claude via Model Context Protocol.
"""
from http.server import BaseHTTPRequestHandler
from upstash_redis import Redis
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
import json
import os

MCP_SECRET = os.environ.get("MCP_SECRET", "")
EXERCISE_DAYS_PER_WEEK = os.environ.get("EXERCISE_DAYS_PER_WEEK", "")

redis = Redis(
    url=os.environ.get("UPSTASH_REDIS_REST_URL"),
    token=os.environ.get("UPSTASH_REDIS_REST_TOKEN")
)


def check_secret(path: str) -> bool:
    if not MCP_SECRET:
        return True
    query = parse_qs(urlparse(path).query)
    return query.get("key", [""])[0] == MCP_SECRET


def parse_exercise_routine() -> dict:
    """Parse exercise routine from env var."""
    if not EXERCISE_DAYS_PER_WEEK:
        return {}
    routine = {}
    for item in EXERCISE_DAYS_PER_WEEK.split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            try:
                routine[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return routine


def get_health_data(date_key: str) -> dict:
    data = redis.get(f"health:{date_key}")
    return json.loads(data) if data else {}


def get_cumulative_total(metric_data: dict) -> int:
    """Extract total from cumulative metric. Handles both storage formats."""
    if not metric_data:
        return 0
    if "total" in metric_data:
        return metric_data["total"]
    if "avg" in metric_data and "count" in metric_data:
        return round(metric_data["avg"] * metric_data["count"])
    return 0


def get_exercise_key(data: dict) -> str:
    """Handle iOS Shortcut naming quirk. Some configs have trailing space."""
    if "exercise " in data:
        return "exercise "
    return "exercise"


def extract_day_metrics(data: dict) -> dict:
    """
    Extract all health metrics from a day's data.
    Single source of truth for field extraction across all tools.
    """
    if not data:
        return None

    metrics = {}

    # HRV
    if "hrv" in data and data["hrv"].get("avg"):
        metrics["hrv"] = round(data["hrv"]["avg"], 1)

    # Heart rate
    if "heartRate" in data:
        hr = data["heartRate"]
        if hr.get("min"):
            metrics["resting_hr"] = round(hr["min"], 1)
        if "hr_zones" in hr and hr["hr_zones"].get("zone_pct"):
            metrics["hr_zones"] = hr["hr_zones"]["zone_pct"]

    # Sleep
    if "sleep" in data:
        sleep = data["sleep"]
        metrics["sleep"] = {
            "quality": sleep.get("quality"),
            "fragmentation_pct": sleep.get("fragmentation_pct"),
            "has_deep": sleep.get("has_deep"),
            "has_rem": sleep.get("has_rem")
        }

    # Exercise minutes
    exercise_key = get_exercise_key(data)
    if exercise_key in data:
        metrics["exercise_min"] = get_cumulative_total(data[exercise_key])

    # Steps
    if "steps" in data:
        metrics["steps"] = get_cumulative_total(data["steps"])

    # Active calories
    if "activeEnergy" in data:
        metrics["active_calories"] = get_cumulative_total(data["activeEnergy"])

    # Mindful minutes
    if "mindful" in data:
        metrics["mindful_min"] = get_cumulative_total(data["mindful"])

    # Respiratory rate
    if "respRate" in data and data["respRate"].get("avg"):
        metrics["respiratory_rate"] = round(data["respRate"]["avg"], 1)

    return metrics if metrics else None


def get_hrv_baseline(days: int = 14) -> dict:
    """Calculate HRV baseline from recent history."""
    hrv_values = []
    for i in range(1, days + 1):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        data = get_health_data(date)
        if data and "hrv" in data and data["hrv"].get("avg"):
            hrv_values.append(data["hrv"]["avg"])
    if not hrv_values:
        return {"baseline": None, "days": 0}
    return {
        "baseline": round(sum(hrv_values) / len(hrv_values), 1),
        "days": len(hrv_values)
    }


# MCP Tools

def tool_get_today() -> str:
    """Get all raw health metrics for today."""
    date_key = datetime.now().strftime("%Y-%m-%d")
    data = get_health_data(date_key)
    if not data:
        return json.dumps({"error": "No data synced today. Run iOS shortcuts."})
    return json.dumps(data, indent=2)


def tool_get_trends(days: int = 7) -> str:
    """Get health metrics over multiple days."""
    results = {}
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        data = get_health_data(date)
        metrics = extract_day_metrics(data)
        if metrics:
            results[date] = metrics
    if not results:
        return json.dumps({"error": f"No data for last {days} days."})
    return json.dumps(results, indent=2)


def tool_get_recovery_status() -> str:
    """Get recovery status with baseline comparisons and recent history."""
    date_key = datetime.now().strftime("%Y-%m-%d")
    data = get_health_data(date_key)
    baseline = get_hrv_baseline()

    status = {
        "date": date_key,
        "weekly_routine": parse_exercise_routine() or None
    }

    # Today's metrics (if synced)
    today_metrics = extract_day_metrics(data)
    if today_metrics:
        status["today"] = today_metrics

        # Add HRV baseline comparison if available
        if "hrv" in today_metrics and baseline.get("baseline"):
            hrv = today_metrics["hrv"]
            status["hrv_vs_baseline"] = {
                "today": hrv,
                "baseline": baseline["baseline"],
                "baseline_days": baseline["days"],
                "pct_diff": round(((hrv - baseline["baseline"]) / baseline["baseline"]) * 100)
            }

    # Recent days for pattern analysis
    recent_days = {}
    for i in range(1, 4):
        day_key = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        day_data = get_health_data(day_key)
        metrics = extract_day_metrics(day_data)
        if metrics:
            recent_days[f"day_minus_{i}"] = metrics
    if recent_days:
        status["recent_days"] = recent_days

    return json.dumps(status, indent=2)


# Tool definitions for MCP

TOOLS = [
    {
        "name": "get_today",
        "description": "Get raw health data for today. Returns unprocessed data as stored.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_trends",
        "description": "Get health metrics over multiple days: HRV, resting HR, HR zones, sleep, exercise minutes, steps, active calories, mindful minutes, respiratory rate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Number of days (default 7)"}
            },
            "required": []
        }
    },
    {
        "name": "get_recovery_status",
        "description": "Get comprehensive recovery data: today's metrics (HRV, resting HR, HR zones, sleep, exercise, steps, calories, mindful minutes, respiratory rate) with HRV baseline comparison, plus last 3 days with full metrics for trend analysis. Includes weekly exercise routine.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    }
]


def handle_tool_call(name: str, args: dict) -> str:
    if name == "get_today":
        return tool_get_today()
    elif name == "get_trends":
        return tool_get_trends(args.get("days", 7))
    elif name == "get_recovery_status":
        return tool_get_recovery_status()
    return json.dumps({"error": f"Unknown tool: {name}"})


class handler(BaseHTTPRequestHandler):
    def send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if not check_secret(self.path):
            self.send_json({"error": "unauthorized"}, 401)
            return
        self.send_json({
            "name": "health",
            "version": "1.0.0",
            "description": "Personal health data from Apple Watch via iOS Shortcuts",
            "tools": TOOLS
        })

    def do_POST(self):
        if not check_secret(self.path):
            self.send_json({"error": "unauthorized"}, 401)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length).decode("utf-8"))

        method = body.get("method", "")
        req_id = body.get("id")

        if method == "initialize":
            self.send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "health", "version": "1.0.0"}
                }
            })
        elif method == "tools/list":
            self.send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS}
            })
        elif method == "tools/call":
            params = body.get("params", {})
            result = handle_tool_call(params.get("name", ""), params.get("arguments", {}))
            self.send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": result}]}
            })
        else:
            self.send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}
            })

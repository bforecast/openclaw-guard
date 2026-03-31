import json
import logging
import sqlite3
import datetime
from pathlib import Path
from mitmproxy import http

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LLM-MitM-Gateway")

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
DB_PATH = LOG_DIR / "security_audit.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                host TEXT,
                method TEXT,
                path TEXT,
                prompt_content TEXT,
                action TEXT,
                reason TEXT
            )
        """)
init_db()

def log_to_db(host, method, path, prompt_content, action, reason=""):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO audit_logs (timestamp, host, method, path, prompt_content, action, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.datetime.now().isoformat(), host, method, path, prompt_content, action, reason)
        )

class SecurityGuardAddon:
    def request(self, flow: http.HTTPFlow):
        # Only intercept LLM domains. We allow OpenClaw to freely reach anything else like github.
        if "api.openai.com" in flow.request.pretty_host or "api.anthropic.com" in flow.request.pretty_host:
            if flow.request.method == "POST":
                if flow.request.content:
                    try:
                        payload = json.loads(flow.request.content)
                        messages = payload.get("messages", [])
                        prompt_text = ""
                        for msg in messages:
                            prompt_text += msg.get("content", "") + "\n"
                        
                        prompt_lower = prompt_text.lower()
                        bad_words = ["rm -rf", "delete everything", "drop table", "upload all files to"]
                        triggered = [bw for bw in bad_words if bw in prompt_lower]
                        
                        if triggered:
                            logger.warning(f"Blocked malicious request to {flow.request.pretty_host}. Triggered: {triggered}")
                            log_to_db(flow.request.pretty_host, flow.request.method, flow.request.path, prompt_text, "BLOCKED", f"Rules: {triggered}")
                            flow.response = http.Response.make(
                                403,
                                b'{"error": "Blocked by MITM Security Guardrail"}',
                                {"Content-Type": "application/json"}
                            )
                            return
                        else:
                            logger.info(f"Allowed safe request to {flow.request.pretty_host}")
                            log_to_db(flow.request.pretty_host, flow.request.method, flow.request.path, prompt_text, "ALLOWED", "")
                    
                    except json.JSONDecodeError:
                        logger.error("Failed to decode JSON payload in intercepted stream")

addons = [SecurityGuardAddon()]

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
import logging
from typing import Dict, List
import os
from pymongo import MongoClient
from groq import Groq
from dotenv import load_dotenv
import requests
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Initialize MongoDB client
client = MongoClient(os.getenv("MONGODB_URI"))
db = client[os.getenv("MONGODB_DB")]

# Initialize Groq client
def get_groq_client():
    from groq import Groq
    return Groq(api_key=os.getenv("GROQ_API_KEY"))

class TaskMonitor:
    def __init__(self):
        self.task_status: Dict[str, Dict] = {}
        self.webhook_status: Dict[str, Dict] = {
            "last_check": None,
            "status": "unknown",
            "response_time": None,
            "error_count": 0
        }

    def update_task_status(self, task_name: str, status: str, error: str = None):
        self.task_status[task_name] = {
            "last_run": datetime.now(),
            "status": status,
            "error": error
        }
        if status == "error":
            self.notify_error(task_name, error)

    def notify_error(self, task_name: str, error: str):
        # TODO: Integrate with an email/Slack notification system
        logger.error(f"Task {task_name} failed: {error}")

    def get_task_status(self) -> Dict:
        return self.task_status

    def check_webhook(self):
        try:
            start_time = time.time()
            response = requests.get(os.getenv("WEBHOOK_URL", "http://localhost:8000/health"))
            response_time = time.time() - start_time
            self.webhook_status = {
                "last_check": datetime.now(),
                "status": "up" if response.status_code == 200 else "down",
                "response_time": response_time,
                "error_count": 0 if response.status_code == 200 else self.webhook_status["error_count"] + 1
            }
            if response.status_code != 200:
                self.notify_error("webhook", f"Status code: {response.status_code}")
        except Exception as e:
            self.webhook_status["error_count"] += 1
            self.notify_error("webhook", str(e))

    def get_webhook_status(self) -> Dict:
        return self.webhook_status

# Initialize scheduler
scheduler = AsyncIOScheduler()

# Initialize task monitor
monitor = TaskMonitor()

async def generate_daily_summary():
    """Generate daily summaries for all active groups."""
    try:
        groups = list(db.groups.find({"is_active": True}))
        for group in groups:
            group_sid = group["group_sid"]
            yesterday = time.time() - 86400
            messages = list(db.processed_messages.find({
                "group_sid": group_sid,
                "timestamp": {"$gte": yesterday}
            }))
            if not messages:
                continue
            text_to_summarize = "\n".join([
                f"{msg.get('from', 'Unknown')}: {msg.get('body', '')}" for msg in messages if msg.get('body')
            ])
            response = get_groq_client().chat.completions.create(
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that summarizes group conversations."},
                    {"role": "user", "content": f"Summarize this group conversation:\n{text_to_summarize}"}
                ],
                model="mixtral-8x7b-32768",
                temperature=0.3,
                max_tokens=150
            )
            summary = response.choices[0].message.content.strip()
            summary_doc = {
                "group_sid": group_sid,
                "date": datetime.now().date(),
                "summary": summary,
                "message_count": len(messages)
            }
            db.daily_summaries.insert_one(summary_doc)
            logger.info(f"Generated daily summary for group {group_sid}")
        monitor.update_task_status("daily_summary", "success")
    except Exception as e:
        monitor.update_task_status("daily_summary", "error", str(e))
        logger.error(f"Error in daily summary task: {str(e)}")

async def perform_database_maintenance():
    """Archive/delete old messages and clean up old summaries."""
    try:
        # Archive messages older than 30 days
        thirty_days_ago = time.time() - 30 * 86400
        old_messages = db.processed_messages.find({"timestamp": {"$lt": thirty_days_ago}})
        for msg in old_messages:
            db.archived_messages.insert_one(msg)
            db.processed_messages.delete_one({"_id": msg["_id"]})
        # Delete summaries older than 90 days
        ninety_days_ago = datetime.now() - timedelta(days=90)
        db.daily_summaries.delete_many({"date": {"$lt": ninety_days_ago.date()}})
        monitor.update_task_status("database_maintenance", "success")
        logger.info("Database maintenance completed.")
    except Exception as e:
        monitor.update_task_status("database_maintenance", "error", str(e))
        logger.error(f"Error in database maintenance: {str(e)}")

async def detect_inactive_groups():
    """Detect groups with no messages in the last 7 days."""
    try:
        seven_days_ago = time.time() - 7 * 86400
        groups = list(db.groups.find({"is_active": True}))
        inactive_groups = []
        for group in groups:
            group_sid = group["group_sid"]
            recent_msg = db.processed_messages.find_one({
                "group_sid": group_sid,
                "timestamp": {"$gte": seven_days_ago}
            })
            if not recent_msg:
                inactive_groups.append(group_sid)
                db.groups.update_one({"group_sid": group_sid}, {"$set": {"is_active": False}})
                logger.info(f"Group {group_sid} marked as inactive.")
        monitor.update_task_status("inactive_group_detection", "success")
        logger.info(f"Inactive groups detected: {inactive_groups}")
    except Exception as e:
        monitor.update_task_status("inactive_group_detection", "error", str(e))
        logger.error(f"Error in inactive group detection: {str(e)}")

def setup_scheduled_tasks():
    """Set up all scheduled tasks."""
    # Daily summary generation (midnight)
    scheduler.add_job(
        generate_daily_summary,
        CronTrigger(hour=0, minute=0),
        id='daily_summary'
    )
    
    # Database maintenance (weekly)
    scheduler.add_job(
        perform_database_maintenance,
        CronTrigger(day_of_week='sun', hour=2),
        id='db_maintenance'
    )
    
    # Inactive group detection (daily)
    scheduler.add_job(
        detect_inactive_groups,
        CronTrigger(hour=0, minute=30),
        id='inactive_groups'
    )
    
    # Webhook monitoring (every 5 minutes)
    scheduler.add_job(
        monitor.check_webhook,
        CronTrigger(minute='*/5'),
        id='webhook_monitor'
    )
    
    scheduler.start()
    logger.info("Scheduled tasks initialized")

__all__ = ['scheduler', 'monitor', 'setup_scheduled_tasks'] 
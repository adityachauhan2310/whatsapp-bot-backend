"""
FastAPI Backend Setup Instructions:

1. Install dependencies:
   pip install -r requirements.txt

2. Run the backend:
   uvicorn main:app --reload --port 8000
uvicorn main:app --reload --port 8000
The server will start at http://localhost:8000
API documentation will be available at http://localhost:8000/docs
"""

import os
from dotenv import load_dotenv
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    BackgroundTasks,
    Depends,
    status,
    Response,
    Query,
    Body,
    File,
    UploadFile,
    Path,
)
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Optional
import time
from datetime import datetime, timedelta
from pydantic import BaseModel, EmailStr
from utils import (
    process_message,
    get_processed_messages,
    transcribe_voice,
    summarize_text,
)
from motor.motor_asyncio import AsyncIOMotorClient
from models import (
    GroupConfig,
    GroupCreate,
    GroupUpdate,
    FilterParams,
    PaginationParams,
    GroupStats,
    MessageType,
    UserRole,
    ParticipantCreate,
    Participant,
    ParticipantRole,
    Message,
    MessageStatus,
)
from models import UserOut
from pymongo.errors import OperationFailure, ConnectionFailure
from pymongo import ASCENDING, DESCENDING
from auth import (
    Token,
    UserCreate,
    UserInDB,
    get_password_hash,
    verify_password,
    create_access_token,
    get_current_user,
    get_current_active_user,
    get_current_admin_user,
    create_refresh_token,
    add_refresh_token,
    remove_refresh_token,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    generate_token,
    send_verification_email,
    login_for_access_token,
    login_json,
)
from scheduler import monitor, setup_scheduled_tasks
from fastapi.security import OAuth2PasswordRequestForm
from pathlib import Path as OSPath
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import holidays
import groq
import logging
from twilio.rest import Client as TwilioClient
from jose import JWTError, jwt
from pymongo import errors
import shutil
from ai_prompts import (
    group_summary_prompt,
    contextual_reply_system_prompt,
    contextual_reply_user_prompt,
)
from bson import ObjectId
import re
import random
import certifi

# Set up logging to file
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.FileHandler("backend.log"),
              logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Check required environment variables
required_env_vars = [
    "TWILIO_AUTH_TOKEN",
    "TWILIO_ACCOUNT_SID",
    "MONGODB_URI",
    "MONGODB_DB",
]

missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(missing_vars)}")

# Twilio credentials from .env
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")

# Twilio WhatsApp sender (from) number from environment
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")

# ... after loading env vars ...
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB")

# Simplified MongoDB connection with minimal required parameters
client = AsyncIOMotorClient(MONGODB_URI)
db = client[MONGODB_DB]

# Create text index for keyword search
try:
    # Create a compound text index on body and keywords fields
    db.processed_messages.create_index([("body", "text"),
                                        ("keywords", "text")],
                                       name="message_text_search")
    print("Successfully created text index for message search")
except OperationFailure as e:
    # If index already exists, this error will be raised
    if "already exists" not in str(e):
        print(f"Error creating text index: {str(e)}")
except Exception as e:
    print(f"Unexpected error creating text index: {str(e)}")

# Create additional indexes for analytics queries
try:
    # Index for messages collection to improve analytics queries
    db.messages.create_index([("group_id", ASCENDING),
                              ("timestamp", DESCENDING)],
                             name="group_time_idx")
    db.messages.create_index([("sender", ASCENDING),
                              ("timestamp", DESCENDING)],
                             name="sender_time_idx")
    db.messages.create_index([("message_type", ASCENDING)],
                             name="message_type_idx")

    # Index for group_participants collection
    db.group_participants.create_index([("group_id", ASCENDING),
                                        ("role", ASCENDING)],
                                       name="group_role_idx")
    db.group_participants.create_index([("phone_number", ASCENDING)],
                                       name="participant_phone_idx")

    # Index for users collection
    db.users.create_index([("username", ASCENDING)],
                          name="username_idx",
                          unique=True)
    db.users.create_index([("email", ASCENDING)],
                          name="email_idx",
                          unique=True)

    # Index for audit_logs collection
    db.audit_logs.create_index([("timestamp", DESCENDING)],
                               name="audit_time_idx")
    db.audit_logs.create_index([("action", ASCENDING),
                                ("timestamp", DESCENDING)],
                               name="audit_action_idx")

    print("Successfully created analytics indexes")
except OperationFailure as e:
    if "already exists" not in str(e):
        print(f"Error creating analytics indexes: {str(e)}")
except Exception as e:
    print(f"Unexpected error creating analytics indexes: {str(e)}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic models
class SearchRequest(BaseModel):
    keyword: str


class TextRequest(BaseModel):
    text: str


class ParticipantAddRequest(BaseModel):
    user_id: str  # or phone_number
    name: str
    role: str  # 'team', 'client', 'leader'


class ParticipantRoleUpdateRequest(BaseModel):
    new_role: str  # 'team', 'client', 'leader'


class HolidayCreateRequest(BaseModel):
    date: str
    description: Optional[str] = None


class WorkingHoursRequest(BaseModel):
    start: str  # e.g. '09:00'
    end: str  # e.g. '18:00'


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


# Background task to process incoming messages
async def process_incoming_message(message_data: Dict):
    # Simulate processing delay
    time.sleep(1)
    # Store message in MongoDB
    db.messages.insert_one(message_data)
    print(f"Processed and stored message: {message_data}")


# Helper to check DB connection
def ensure_db():
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=
            "Database is temporarily unavailable. Please try again later.",
        )


# Create a test admin user if one doesn't exist
async def create_test_admin():
    if db is None:
        print(
            "Database connection not available, skipping test admin creation")
        return

    # Check if admin exists already
    admin = await db.users.find_one({"username": "admin"})
    if admin:
        print("Admin user already exists")
        return

    # Create admin user
    try:
        hashed_password = get_password_hash("Admin123!")
        admin_doc = {
            "username": "admin",
            "email": "admin@example.com",
            "hashed_password": hashed_password,
            "full_name": "Administrator",
            "role": "admin",
            "is_active": True,
            "is_verified": True,  # Already verified
            "refresh_tokens": [],
        }
        await db.users.insert_one(admin_doc)
        print("Created default admin user: username=admin, password=Admin123!")
    except Exception as e:
        print(f"Error creating admin user: {str(e)}")


@app.get("/health")
async def health_check():
    try:
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Health check failed: {str(e)}")


@app.post("/webhook/twilio")
async def twilio_webhook(request: Request, background_tasks: BackgroundTasks):
    """Twilio webhook endpoint for processing incoming WhatsApp messages."""
    ensure_db()
    try:
        # TEST BYPASS: Skip signature check if ?test=true is present
        skip_signature = request.query_params.get("test") == "true"
        print("Twilio signature bypass:", skip_signature)

        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        form_data = await request.form()
        print("[DEBUG] Twilio form_data received:", dict(form_data))
        url = str(request.url)
        signature = request.headers.get("X-Twilio-Signature", "")

        if not skip_signature:
            if not validator.validate(url, form_data, signature):
                print("Twilio signature validation failed")
                raise HTTPException(status_code=403,
                                    detail="Invalid Twilio signature")
        else:
            print("Signature check bypassed for testing.")

        # Extract message data
        message_body = form_data.get("Body", None)
        if not message_body:
            print("[ERROR] No 'Body' field in form_data! Full form_data:",
                  dict(form_data))
            raise HTTPException(status_code=400,
                                detail="No message body provided")

        # Extract group information
        group_sid = form_data.get("GroupSid")
        if not group_sid:
            print(
                "[WARNING] No GroupSid in message. This might be a direct message."
            )
            group_sid = "direct_message"  # Default for direct messages

        # Check if today is a holiday or weekend using company_holidays collection
        today = datetime.now().date()
        is_weekend = today.weekday() >= 5  # 5 = Saturday, 6 = Sunday
        # Query company_holidays for today
        holiday_doc = db.company_holidays.find_one({"date": today.isoformat()})
        is_company_holiday = holiday_doc is not None
        # --- Working hours check ---
        wh = db.company_working_hours.find_one()
        if wh:
            now = datetime.now().time()
            from datetime import datetime as dt

            start = dt.strptime(wh["start"], "%H:%M").time()
            end = dt.strptime(wh["end"], "%H:%M").time()
            if not (start <= now <= end):
                auto_reply = {
                    "group_id": group_sid,
                    "client_number": form_data.get("From"),
                    "reason": "after_hours",
                    "reply_text":
                    f"Our team is available from {start.strftime('%H:%M')} to {end.strftime('%H:%M')}. We will respond to your message during working hours.",
                    "timestamp": datetime.now(),
                }
                db.auto_replies.insert_one(auto_reply)
                response = MessagingResponse()
                response.message(
                    f"Thank you for your message. Our working hours are {start.strftime('%H:%M')} to {end.strftime('%H:%M')}. We will respond during business hours."
                )
                return str(response)
        # --- End working hours check ---
        if is_weekend or is_company_holiday:
            # Create auto-reply document
            auto_reply = {
                "group_id": group_sid,
                "client_number": form_data.get("From"),
                "reason": "holiday",
                "reply_text":
                "We are currently closed for the weekend/holiday. We will respond to your message during our next business day.",
                "timestamp": datetime.now(),
            }
            # Insert into auto_replies collection
            db.auto_replies.insert_one(auto_reply)
            # Return early with a simple response
            response = MessagingResponse()
            return str(response)

        # Continue with normal message processing...
        # Create message document
        message_doc = Message(
            group_id=group_sid,
            sender=form_data.get("From"),
            timestamp=datetime.now(),
            body=message_body,
            status=MessageStatus.RECEIVED,
            message_type=MessageType.TEXT,
            media_url=form_data.get("MediaUrl0"),
            raw_payload=dict(form_data),
        ).dict()

        # Store message in MongoDB
        result = db.messages.insert_one(message_doc)
        print(f"Stored message with ID: {result.inserted_id}")

        # Determine message type
        if form_data.get("MediaUrl0"):
            content_type = form_data.get("MediaContentType0", "")
            if content_type.startswith("audio"):
                message_doc["message_type"] = MessageType.VOICE
            elif content_type.startswith(
                    "application") or content_type.startswith("document"):
                message_doc["message_type"] = MessageType.DOCUMENT
            else:
                message_doc["message_type"] = MessageType.MEDIA

            # Update message type in database
            db.messages.update_one(
                {"_id": result.inserted_id},
                {"$set": {
                    "message_type": message_doc["message_type"]
                }},
            )

        # Queue for background processing
        background_tasks.add_task(process_message, message_doc)

        # --- Auto-create group and participant on new group message ---
        if group_sid != "direct_message":
            # Check if group exists
            group = await db.groups.find_one({"group_sid": group_sid})
            if not group:
                group_doc = {
                    "group_sid":
                    group_sid,
                    "name": (f"WhatsApp Group {group_sid[-4:]}"
                             if group_sid else "Unknown Group"),
                    "description":
                    None,
                    "is_active":
                    True,
                    "keywords": [],
                    "notification_settings": {},
                }
                db.groups.insert_one(group_doc)
            # Check if participant exists
            sender_number = form_data.get("From")
            participant = db.group_participants.find_one({
                "group_id":
                group_sid,
                "phone_number":
                sender_number
            })
            if not participant:
                participant_doc = {
                    "group_id": group_sid,
                    "phone_number": sender_number,
                    "name":
                    sender_number,  # Default to number; can be updated later
                    "role": "client",
                    "added_at": datetime.now().isoformat(),
                    "is_active": True,
                }
                db.group_participants.insert_one(participant_doc)
        # --- End auto-create logic ---

        # Return TwiML response
        response = MessagingResponse()
        return str(response)

    except Exception as e:
        import traceback

        print("Webhook error:", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhook/twilio-status")
async def twilio_status_webhook(request: Request):
    ensure_db()
    data = await request.form()
    message_sid = data.get("MessageSid")
    event_type = data.get("MessageStatus")  # e.g., 'delivered', 'sent', etc.
    if message_sid and event_type == "delivered":
        # Find the message with escalation.alert_sid == message_sid
        result = db.messages.update_one(
            {"escalation.alert_sid": message_sid},
            {"$set": {
                "escalation.delivered": True
            }},
        )
        if result.modified_count > 0:
            print(f"Marked escalation {message_sid} as delivered.")
    return {"status": "ok"}


@app.post("/api/groups", response_model=GroupConfig)
async def create_group(
    group: GroupCreate,
    current_user: UserInDB = Depends(get_current_active_user)):
    """Create a new WhatsApp group configuration."""
    ensure_db()
    try:
        group_config = GroupConfig(
            group_sid=f"GRP_{int(time.time())}",
            name=group.name,
            description=group.description,
            keywords=group.keywords or [],
            notification_settings=group.notification_settings or {},
        )

        db.groups.insert_one(group_config.dict())
        return group_config
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/groups", response_model=List[GroupConfig])
async def list_groups():
    cursor = db.groups.find()
    groups = await cursor.to_list(length=100)
    return [GroupConfig(**group) for group in groups]


@app.get("/api/groups/{group_sid}", response_model=GroupConfig)
async def get_group(group_sid: str):
    ensure_db()
    try:
        group = await db.groups.find_one({"group_sid": group_sid})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        return GroupConfig(**group)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/groups/{group_sid}", response_model=GroupConfig)
async def update_group(group_sid: str, group_update: GroupUpdate):
    ensure_db()
    try:
        update_data = {
            k: v
            for k, v in group_update.dict().items() if v is not None
        }
        if not update_data:
            raise HTTPException(status_code=400,
                                detail="No update data provided")
        result = db.groups.find_one_and_update({"group_sid": group_sid},
                                               {"$set": update_data},
                                               return_document=True)
        if not result:
            raise HTTPException(status_code=404, detail="Group not found")
        return GroupConfig(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/groups/{group_sid}")
async def delete_group(group_sid: str):
    ensure_db()
    try:
        result = db.groups.delete_one({"group_sid": group_sid})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Group not found")
        return {"status": "success", "message": "Group deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/groups/{group_sid}/stats", response_model=GroupStats)
async def get_group_stats(group_sid: str):
    """Get statistics for a specific group."""
    ensure_db()
    try:
        # Get total messages
        total_messages = await db.processed_messages.count_documents(
            {"group_sid": group_sid})

        # Get message type distribution
        message_types_pipeline = [{
            "$match": {
                "group_sid": group_sid
            }
        }, {
            "$group": {
                "_id": "$type",
                "count": {
                    "$sum": 1
                }
            }
        }]
        message_types_cursor = db.processed_messages.aggregate(
            message_types_pipeline)
        message_types_result = await message_types_cursor.to_list(length=100)
        message_type_dist = {
            doc["_id"]: doc["count"]
            for doc in message_types_result
        }

        # Get unique active users
        distinct_cursor = db.processed_messages.distinct(
            "from", {"group_sid": group_sid})
        active_users_list = await distinct_cursor
        active_users = len(active_users_list)

        # Get last activity
        last_message = await db.processed_messages.find_one(
            {"group_sid": group_sid}, sort=[("timestamp", DESCENDING)])
        last_activity = (datetime.fromtimestamp(last_message["timestamp"])
                         if last_message else None)

        # Get keywords (from group config)
        group = await db.groups.find_one({"group_sid": group_sid})
        keywords = group.get("keywords", []) if group else []

        return GroupStats(
            total_messages=total_messages,
            message_types=message_type_dist,
            keywords=keywords,
            active_users=active_users,
            last_activity=last_activity,
        )
    except Exception as e:
        import traceback
        print(f"Error in get_group_stats: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/messages/processed")
async def get_processed_messages_endpoint(
        filters: FilterParams = Depends(),
        pagination: PaginationParams = Depends()):
    ensure_db()
    try:
        # Build query
        query = {}
        if filters.group_sid:
            query["group_sid"] = filters.group_sid
        if filters.message_type:
            query["type"] = filters.message_type
        if filters.start_date:
            query["timestamp"] = {"$gte": filters.start_date.timestamp()}
        if filters.end_date:
            query.setdefault("timestamp",
                             {})["$lte"] = filters.end_date.timestamp()
        if filters.keyword:
            query["$text"] = {"$search": filters.keyword}
        # Build sort
        sort = []
        if pagination.sort_by:
            sort_direction = ASCENDING if pagination.sort_order == "asc" else DESCENDING
            sort.append((pagination.sort_by, sort_direction))
        sort.append(("timestamp", DESCENDING))
        # Get total count
        total = await db.processed_messages.count_documents(query)
        # Get paginated results
        skip = (pagination.page - 1) * pagination.page_size
        cursor = db.processed_messages.find(query).sort(sort).skip(skip).limit(
            pagination.page_size)
        messages = await cursor.to_list(length=pagination.page_size)

        # Convert ObjectId to string for JSON serialization
        for msg in messages:
            if "_id" in msg:
                msg["_id"] = str(msg["_id"])
        # Ensure body/content is present
        for msg in messages:
            if "body" not in msg:
                if "content" in msg:
                    msg["body"] = msg["content"]
                elif "text" in msg:
                    msg["body"] = msg["text"]
                elif "transcription" in msg:
                    msg["body"] = msg["transcription"]
                elif "notes" in msg:
                    msg["body"] = msg["notes"]
                else:
                    msg["body"] = None
        return {
            "messages":
            messages,
            "total":
            total,
            "page":
            pagination.page,
            "page_size":
            pagination.page_size,
            "total_pages":
            (total + pagination.page_size - 1) // pagination.page_size,
        }
    except Exception as e:
        import traceback

        print("Error in get_processed_messages_endpoint:",
              traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/messages/process_message")
async def process_message_alias(filters: FilterParams = Depends(),
                                pagination: PaginationParams = Depends()):
    ensure_db()
    try:
        return await get_processed_messages_endpoint(filters, pagination)
    except Exception as e:
        import traceback

        print("Error in process_message_alias:", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/summaries/daily")
@app.get("/api/summary/daily")  # Add an alias to match frontend URL
async def get_daily_summaries(
    group_sid: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    ensure_db()
    try:
        query = {}
        if group_sid:
            query["group_sid"] = group_sid
        if start_date:
            query["date"] = {"$gte": start_date.date().isoformat()}
        if end_date:
            query.setdefault("date", {})["$lte"] = end_date.date().isoformat()

        # Find existing summaries
        cursor = db.daily_summaries.find(query).sort("date", DESCENDING)
        summaries = await cursor.to_list(length=100)

        # If no summaries found, provide mock data
        if not summaries:
            logger.info("No summaries found, using mock data")
            summaries = mock_daily_summaries()

            # Filter mock data according to query parameters if needed
            if group_sid:
                summaries = [
                    s for s in summaries if s.get("group_sid") == group_sid
                ]

            if start_date:
                summaries = [
                    s for s in summaries
                    if s.get("date") >= start_date.date().isoformat()
                ]

            if end_date:
                summaries = [
                    s for s in summaries
                    if s.get("date") <= end_date.date().isoformat()
                ]

        # Convert ObjectId to string for JSON serialization
        for summary in summaries:
            if "_id" in summary:
                summary["_id"] = str(summary["_id"])

        return {"summaries": summaries}
    except Exception as e:
        logger.error(f"Error fetching daily summaries: {str(e)}")
        return {"summaries": mock_daily_summaries(), "error": str(e)}


@app.post("/api/summary/search")
async def search_summaries(request: SearchRequest):
    ensure_db()
    try:
        if not request.keyword.strip():
            raise HTTPException(status_code=400,
                                detail="Keyword cannot be empty")

        # Simulate a small delay
        time.sleep(0.8)

        # Mock search results
        results = [
            {
                "id": "1",
                "title": f"Sample result for {request.keyword}",
                "date": "2024-02-20",
                "summary":
                f"This is a sample summary containing the keyword {request.keyword}",
                "relevance_score": 0.95,
            },
            {
                "id": "2",
                "title": f"Another result about {request.keyword}",
                "date": "2024-02-19",
                "summary":
                f"Here's another summary mentioning {request.keyword}",
                "relevance_score": 0.85,
            },
        ]

        return {"results": results}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@app.get("/api/media/latest")
async def get_latest_media():
    ensure_db()
    try:
        cursor = db.media.find({}, {"_id": 0})
        media_items = await cursor.to_list(length=100)  # Limit to 100 items
        return {"media_items": media_items}
    except Exception as e:
        import traceback
        print("Error in get_latest_media:", traceback.format_exc())
        raise HTTPException(status_code=500,
                            detail=f"Failed to fetch media items: {str(e)}")


@app.post("/api/voice/transcribe")
async def transcribe_voice_endpoint():
    """
    Endpoint to transcribe voice input.
    Returns the transcribed text.
    """
    ensure_db()
    try:
        transcribed_text = transcribe_voice()
        return {"transcription": transcribed_text}
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Transcription failed: {str(e)}")


@app.post("/api/text/summarize")
async def summarize_text_endpoint(request: TextRequest):
    """
    Endpoint to summarize text input.
    Returns the summarized text.
    """
    ensure_db()
    try:
        if not request.text.strip():
            raise HTTPException(status_code=400, detail="Text cannot be empty")

        summary = summarize_text(request.text)
        return {"summary": summary}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Summarization failed: {str(e)}")


@app.post("/api/migrate_media_to_processed")
def migrate_media_to_processed():
    ensure_db()
    try:
        media_docs = list(db.media.find())
        migrated = 0
        for doc in media_docs:
            # Build a processed_message document
            processed_doc = {
                "type": doc.get("type", "media"),
                "media_url": doc.get("url"),
                "file_type": doc.get("type"),
                "title": doc.get("title"),
                "upload_date": doc.get("upload_date"),
                "views": doc.get("views"),
                "timestamp": time.time(),
                "from": "media_migration",
            }
            db.processed_messages.insert_one(processed_doc)
            migrated += 1
        return {"status": "success", "migrated": migrated}
    except Exception as e:
        import traceback

        print("Error in migrate_media_to_processed:", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# Initialize scheduler
scheduler = AsyncIOScheduler()

# Initialize Twilio REST client
_twilio_client = None


def get_twilio_client():
    global _twilio_client
    if _twilio_client is None:
        _twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client


async def notify_leader_via_whatsapp(group_id, message):
    # Find the leader participant for this group
    leader = db.group_participants.find_one({
        "group_id": group_id,
        "role": "leader",
        "is_active": True
    })
    if not leader:
        logger.warning(f"No leader found for group {group_id} to notify.")
        return
    leader_number = leader["phone_number"]
    # Compose notification message
    notification = f"[Escalation] Client message missed by team.\nMessage: {message['body']}\nFrom: {message['sender']}\nTime: {message['timestamp']}"
    # Send WhatsApp message via Twilio
    try:
        client = get_twilio_client()
        client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_FROM}",
            to=f"whatsapp:{leader_number}",
            body=notification,
        )
        logger.info(
            f"Escalation notification sent to leader {leader_number} for group {group_id}"
        )
    except Exception as e:
        logger.error(
            f"Failed to send escalation notification to leader: {str(e)}")


async def check_message_escalation():
    """
    Check for messages that need escalation:
    1. After 1 hour: Mark as ignored and set notification time
    2. After 2 hours: Mark as auto-replied
    3. Lookup leader, send Twilio Conversations notification, store alert_sid
    """
    try:
        current_time = datetime.now()
        one_hour_ago = current_time - timedelta(hours=1)
        two_hours_ago = current_time - timedelta(hours=2)

        query = {
            "timestamp": {
                "$lt": one_hour_ago
            },
            "sender": {
                "$regex": "^whatsapp:"
            },
            "escalation.ignored": {
                "$ne": True
            },
        }

        cursor = db.messages.find(query)
        client_messages = await cursor.to_list(length=100)

        for message in client_messages:
            reply_query = {
                "group_id": message["group_id"],
                "timestamp": {
                    "$gt": message["timestamp"]
                },
                "sender": {
                    "$regex": "^team:"
                },
            }

            team_reply = await db.messages.find_one(reply_query)

            if not team_reply:
                update_data = {
                    "escalation.ignored": True,
                    "escalation.notified_at": current_time,
                }
                if message["timestamp"] < two_hours_ago:
                    update_data["escalation.auto_replied"] = True

                # Lookup leader's phone_number
                leader = await db.group_participants.find_one({
                    "group_id":
                    message["group_id"],
                    "role":
                    "leader",
                    "is_active":
                    True,
                })
                alert_sid = None
                if leader:
                    leader_number = leader.get("phone_number")
                    try:
                        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID,
                                                     TWILIO_AUTH_TOKEN)
                        # Find the conversation for this group (assume friendly_name == group_id)
                        conversations = twilio_client.conversations.conversations.list(
                            friendly_name=message["group_id"])
                        if conversations:
                            conversation_sid = conversations[0].sid
                            notification = (
                                f"[Escalation] Client message missed by team.\n"
                                f"Message: {message['body']}\n"
                                f"From: {message['sender']}\n"
                                f"Time: {message['timestamp']}")
                            msg = twilio_client.conversations.conversations(
                                conversation_sid).messages.create(
                                    author="system", body=notification)
                            alert_sid = msg.sid
                            update_data["escalation.alert_sid"] = alert_sid
                        else:
                            print(
                                f"No Twilio conversation found for group_id {message['group_id']}"
                            )
                    except Exception as twilio_err:
                        print(f"Twilio notification failed: {twilio_err}")
                        update_data["escalation.alert_sid"] = None
                else:
                    print(
                        f"No leader found for group {message['group_id']} to notify."
                    )
                    update_data["escalation.alert_sid"] = None

                await db.messages.update_one({"_id": message["_id"]},
                                             {"$set": update_data})

                print(
                    f"Escalated message {message['_id']} at {current_time}, alert_sid: {alert_sid}"
                )

    except Exception as e:
        print(f"Error in message escalation job: {str(e)}")


@app.on_event("startup")
async def startup_event():
    """Initialize scheduler and other startup tasks"""
    # Start the scheduler
    scheduler.start()

    # Create test admin user
    await create_test_admin()

    # Add the message escalation job
    scheduler.add_job(
        check_message_escalation,
        trigger=IntervalTrigger(minutes=15),
        id="message_escalation",
        replace_existing=True,
    )

    # Setup other scheduled tasks
    setup_scheduled_tasks()


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup tasks on shutdown"""
    scheduler.shutdown()


# Updated generate_daily_statistics function to use async properly
async def generate_daily_statistics():
    """Generate statistics for all groups once per day"""
    logger.info("Running daily statistics generation job")
    try:
        cursor = db.groups.find({"is_active": True})
        groups = await cursor.to_list(length=100)
        stats_generated = 0

        for group in groups:
            group_id = group["group_sid"]

            # Calculate yesterday's statistics
            yesterday = datetime.now() - timedelta(days=1)
            start_of_day = datetime.combine(yesterday.date(),
                                            datetime.min.time())
            end_of_day = datetime.combine(yesterday.date(),
                                          datetime.max.time())

            # Get messages for the day
            query = {
                "group_id": group_id,
                "timestamp": {
                    "$gte": start_of_day,
                    "$lte": end_of_day
                },
            }

            total_messages = await db.messages.count_documents(query)

            # Skip if no activity
            if total_messages == 0:
                continue

            # Message type distribution
            message_types_pipeline = [
                {
                    "$match": query
                },
                {
                    "$group": {
                        "_id": "$message_type",
                        "count": {
                            "$sum": 1
                        }
                    }
                },
            ]
            message_types_cursor = db.messages.aggregate(
                message_types_pipeline)
            message_types_result = await message_types_cursor.to_list(
                length=100)
            message_types = {
                doc["_id"]: doc["count"]
                for doc in message_types_result
            }

            # Active senders
            senders_pipeline = [
                {
                    "$match": query
                },
                {
                    "$group": {
                        "_id": "$sender",
                        "message_count": {
                            "$sum": 1
                        }
                    }
                },
            ]
            senders_cursor = db.messages.aggregate(senders_pipeline)
            senders_result = await senders_cursor.to_list(length=100)

            # Create daily stats document
            stats_doc = {
                "group_id": group_id,
                "group_name": group.get("name"),
                "date": yesterday.date().isoformat(),
                "total_messages": total_messages,
                "message_types": message_types,
                "active_senders": len(senders_result),
                "sender_activity": {
                    doc["_id"]: doc["message_count"]
                    for doc in senders_result
                },
                "generated_at": datetime.now(),
            }

            # Store in daily_stats collection
            await db.group_daily_stats.update_one(
                {
                    "group_id": group_id,
                    "date": yesterday.date().isoformat()
                },
                {"$set": stats_doc},
                upsert=True,
            )

            stats_generated += 1

        logger.info(
            f"Daily statistics generated for {stats_generated} active groups")
    except Exception as e:
        logger.error(f"Error in daily statistics job: {str(e)}")


# Monitoring Endpoints
@app.get("/api/monitor/tasks", response_model=Dict)
async def get_task_status(
        current_user: UserInDB = Depends(get_current_admin_user)):
    """Get status of all scheduled tasks (admin only)."""
    return monitor.get_task_status()


@app.get("/api/monitor/webhook", response_model=Dict)
async def get_webhook_status(
        current_user: UserInDB = Depends(get_current_admin_user)):
    """Get webhook monitoring status (admin only)."""
    return monitor.get_webhook_status()


# --- User Management Endpoints (Admin Only) ---
@app.get("/api/users", response_model=List[UserInDB], tags=["User Management"])
async def list_users(current_user: UserInDB = Depends(get_current_admin_user)):
    users = await db.users.find().to_list(length=None)
    return [UserInDB(**u) for u in users]


@app.get("/api/users/{username}",
         response_model=UserOut,
         tags=["User Management"])
async def get_user_profile(
    username: str, current_user: UserInDB = Depends(get_current_active_user)):
    ensure_db()
    if not username or username == "undefined":
        raise HTTPException(status_code=400, detail="Username is required.")
    if current_user.username != username and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403,
                            detail="Not authorized to view this user")
    user = await db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_data = serialize_user(user)
    return user_data


@app.api_route("/api/users/{username}", methods=["POST", "PATCH", "DELETE"])
@app.api_route("/api/users/{username}/avatar",
               methods=["PUT", "PATCH", "DELETE"])
async def method_not_allowed(request: Request, username: str):
    return JSONResponse(status_code=405,
                        content={"detail": "Method Not Allowed"})


def serialize_user(user):
    # Accepts a dict or Pydantic model
    return {
        "id": (user.get("username") if isinstance(user, dict) else getattr(
            user, "username", None)),
        "username": (user.get("username") if isinstance(user, dict) else
                     getattr(user, "username", None)),
        "email": (user.get("email") if isinstance(user, dict) else getattr(
            user, "email", None)),
        "full_name": (user.get("full_name") if isinstance(user, dict) else
                      getattr(user, "full_name", None)),
        "role":
        (user.get("role", "").upper() if isinstance(user, dict) else getattr(
            user, "role", "").upper()),
        "is_active": (user.get("is_active") if isinstance(user, dict) else
                      getattr(user, "is_active", None)),
        "is_verified": (user.get("is_verified") if isinstance(user, dict) else
                        getattr(user, "is_verified", None)),
        "avatar": (user.get("avatar") if isinstance(user, dict) else getattr(
            user, "avatar", None)),
        # Add other fields as needed
    }


@app.put("/api/users/{user_id}/toggle-status", tags=["User Management"])
async def toggle_user_status(
    user_id: str, current_user: UserInDB = Depends(get_current_admin_user)):
    """
    Toggle a user's active status (admin only).
    """
    ensure_db()
    try:
        # Find user
        user = db.users.find_one({"username": user_id})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Toggle status
        new_status = not user.get("is_active", True)
        result = db.users.update_one({"username": user_id},
                                     {"$set": {
                                         "is_active": new_status
                                     }})

        if result.modified_count == 0:
            raise HTTPException(status_code=500,
                                detail="Failed to update user status")

        return {"username": user_id, "is_active": new_status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/users/{user_id}", tags=["User Management"])
async def delete_user(
    user_id: str, current_user: UserInDB = Depends(get_current_admin_user)):
    """
    Delete a user (admin only).
    """
    ensure_db()
    try:
        # Check if user exists
        user = await db.users.find_one({"username": user_id})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Delete user
        result = await db.users.delete_one({"username": user_id})

        if result.deleted_count == 0:
            raise HTTPException(status_code=500,
                                detail="Failed to delete user")

        # Add to audit log
        audit_log = {
            "action": "delete_user",
            "user_id": user_id,
            "performed_by": current_user.username,
            "timestamp": datetime.now(),
            "details": f"User {user_id} deleted by {current_user.username}",
        }
        await db.audit_logs.insert_one(audit_log)

        return {
            "status": "success",
            "message": f"User {user_id} deleted successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/users/enhanced", response_model=dict, tags=["User Management"])
async def list_users_enhanced(
        role: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
        skip: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
        current_user: UserInDB = Depends(get_current_admin_user),
):
    """
    Get all users with filtering options (admin only).
    """
    ensure_db()
    try:
        # Build query
        query = {}
        if role:
            query["role"] = role
        if is_active is not None:
            query["is_active"] = is_active
        if search:
            query["$or"] = [
                {
                    "username": {
                        "$regex": search,
                        "$options": "i"
                    }
                },
                {
                    "email": {
                        "$regex": search,
                        "$options": "i"
                    }
                },
                {
                    "full_name": {
                        "$regex": search,
                        "$options": "i"
                    }
                },
            ]

        # Get users with pagination
        users = list(db.users.find(query).skip(skip).limit(limit))
        total = db.users.count_documents(query)

        # Convert to UserOut model and exclude sensitive fields
        user_list = []
        for user in users:
            user_data = serialize_user(user)
            user_list.append(user_data)

        return {
            "users": user_list,
            "total": total,
            "page": (skip // limit) + 1,
            "page_size": limit,
            "total_pages": (total + limit - 1) // limit,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- System Status & Analytics Endpoints ---
@app.get("/api/system/status", tags=["System"])
async def system_status(
        current_user: UserInDB = Depends(get_current_admin_user)):
    """Get system status and basic analytics (admin only)."""
    try:
        users_count = await db.users.count_documents({})
        groups_count = await db.groups.count_documents({})
        messages_count = await db.processed_messages.count_documents({})
        active_groups_count = await db.groups.count_documents(
            {"is_active": True})
        inactive_groups_count = await db.groups.count_documents(
            {"is_active": False})
        daily_summaries_count = await db.daily_summaries.count_documents({})

        return {
            "users": users_count,
            "groups": groups_count,
            "messages": messages_count,
            "active_groups": active_groups_count,
            "inactive_groups": inactive_groups_count,
            "daily_summaries": daily_summaries_count,
        }
    except Exception as e:
        import traceback
        print(f"Error in system_status: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/analytics/group/{group_id}", tags=["Analytics"])
async def group_analytics(group_id: str):
    try:
        total_msgs = await db.processed_messages.count_documents(
            {"group_sid": group_id})

        distinct_cursor = db.processed_messages.distinct(
            "from", {"group_sid": group_id})
        users = await distinct_cursor

        last_msg = await db.processed_messages.find_one(
            {"group_sid": group_id}, sort=[("timestamp", DESCENDING)])

        return {
            "group_id":
            group_id,
            "total_messages":
            total_msgs,
            "unique_users":
            len(users),
            "last_activity": (datetime.fromtimestamp(last_msg["timestamp"])
                              if last_msg else None),
        }
    except Exception as e:
        import traceback
        print(f"Error in group_analytics: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/analytics/daily/{group_id}", tags=["Analytics"])
async def get_daily_statistics(
        group_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        current_user: UserInDB = Depends(get_current_active_user),
):
    """
    Get daily statistics for a group within a date range.
    """
    ensure_db()
    try:
        # Parse date range
        start = (datetime.fromisoformat(start_date).date() if start_date else
                 (datetime.now() - timedelta(days=30)).date())
        end = (datetime.fromisoformat(end_date).date()
               if end_date else datetime.now().date())

        # Check if group exists
        group = await db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        # Query daily stats
        query = {
            "group_id": group_id,
            "date": {
                "$gte": start.isoformat(),
                "$lte": end.isoformat()
            },
        }

        stats = list(db.group_daily_stats.find(query).sort("date", ASCENDING))

        # Convert ObjectId to string for JSON serialization
        for stat in stats:
            if "_id" in stat:
                stat["_id"] = str(stat["_id"])

        # Fill in missing dates with zeros
        dates = []
        current_date = start
        while current_date <= end:
            dates.append(current_date.isoformat())
            current_date += timedelta(days=1)

        # Create a map of existing stats by date
        stats_by_date = {stat["date"]: stat for stat in stats}

        # Create complete series with zeros for missing dates
        complete_series = []
        for date in dates:
            if date in stats_by_date:
                complete_series.append(stats_by_date[date])
            else:
                complete_series.append({
                    "group_id": group_id,
                    "date": date,
                    "total_messages": 0,
                    "message_types": {},
                    "active_senders": 0,
                    "sender_activity": {},
                })

        return {
            "group_id": group_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily_stats": complete_series,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/analytics/messages", tags=["Analytics"])
async def message_analytics(
        group_sid: Optional[str] = None,
        start_date: Optional[datetime] = Query(None),
        end_date: Optional[datetime] = Query(None),
        message_type: Optional[str] = None,
        current_user: UserInDB = Depends(get_current_admin_user),
):
    """Get message analytics with filters (admin only)."""
    query = {}
    if group_sid:
        query["group_sid"] = group_sid
    if message_type:
        query["type"] = message_type
    if start_date:
        query["timestamp"] = {"$gte": start_date.timestamp()}
    if end_date:
        query.setdefault("timestamp", {})["$lte"] = end_date.timestamp()
    total = db.processed_messages.count_documents(query)
    return {"total": total, "filters": query}


# --- Advanced Message Search Endpoint ---
@app.get("/api/messages/search", tags=["Messages"])
async def search_messages(
    keyword: Optional[str] = None,
    group_sid: Optional[str] = None,
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    message_type: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    query = {}
    if keyword:
        query["$text"] = {"$search": keyword}
    if group_sid:
        query["group_sid"] = group_sid
    if message_type:
        query["type"] = message_type
    if start_date:
        query["timestamp"] = {"$gte": start_date.timestamp()}
    if end_date:
        query.setdefault("timestamp", {})["$lte"] = end_date.timestamp()
    skip = (page - 1) * page_size
    cursor = db.processed_messages.find(query).sort(
        "timestamp", DESCENDING).skip(skip).limit(page_size)
    messages = await cursor.to_list(length=page_size)
    total = await db.processed_messages.count_documents(query)

    # Ensure body/content is present for /api/messages/search
    for msg in messages:
        if "body" not in msg:
            if "content" in msg:
                msg["body"] = msg["content"]
            elif "text" in msg:
                msg["body"] = msg["text"]
            elif "transcription" in msg:
                msg["body"] = msg["transcription"]
            elif "notes" in msg:
                msg["body"] = msg["notes"]
            else:
                msg["body"] = None

        # Convert ObjectId to string for JSON serialization
        if "_id" in msg:
            msg["_id"] = str(msg["_id"])

    return {
        "messages": messages,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@app.post("/api/token")
async def token_endpoint(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    return await login_for_access_token(request, form_data)


@app.post("/api/login")
async def login_endpoint(request: Request):
    return await login_json(request)


@app.post("/api/register")
async def register_user(user: UserCreate = Body(...)):
    from fastapi.responses import JSONResponse

    try:
        ensure_db()
        # Check for duplicate username or email
        existing_user = await db.users.find_one(
            {"$or": [{
                "username": user.username
            }, {
                "email": user.email
            }]})

        if existing_user:
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "error": "Username or email already exists"
                },
            )

        # Hash the password
        hashed_password = get_password_hash(user.password)

        # Always set role to 'user' for public registration
        role = "user"

        # Generate confirmation token
        confirmation_token = generate_token(48)

        # Create user document
        user_doc = {
            "username": user.username,
            "email": user.email,
            "hashed_password": hashed_password,
            "full_name": user.full_name,
            "role": role,
            "is_active": True,
            "is_verified": False,
            "verification_token": confirmation_token,
            "refresh_tokens": [],
        }

        result = await db.users.insert_one(user_doc)

        # Send confirmation email
        await send_verification_email(user.email, confirmation_token)

        return JSONResponse(
            status_code=201,
            content={
                "success": True,
                "message":
                "Registration successful. Please check your email to confirm your account.",
                "user": serialize_user(user_doc),
            },
        )
    except Exception as e:
        print(f"Error in register_user: {str(e)}")
        return JSONResponse(status_code=400,
                            content={
                                "success": False,
                                "error": str(e)
                            })


# Alias to /api/users/register to match frontend requirements
@app.post("/api/users/register")
async def register_user_alias(user: UserCreate = Body(...)):
    """
    Alias to register_user to match frontend requirements.
    """
    return await register_user(user)


@app.get("/api/confirm-email")
async def confirm_email(token: str):
    from fastapi.responses import JSONResponse

    try:
        user = await db.users.find_one({"verification_token": token})
        if not user:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Invalid or expired confirmation token.",
                },
            )
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "is_verified": True,
                "verification_token": None
            }},
        )
        # Optionally, redirect to a confirmation page
        # from fastapi.responses import RedirectResponse
        # return RedirectResponse(url="https://yourdomain.com/email-confirmed")
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": "Email confirmed! You can now log in.",
            },
        )
    except Exception as e:
        print(f"Error confirming email: {str(e)}")
        return JSONResponse(status_code=400,
                            content={
                                "success": False,
                                "error": str(e)
                            })


@app.get("/api/media/proxy")
async def proxy_media(url: str):
    try:
        # Implementation of proxy_media function
        # This function should return a response with the media content
        # For now, we'll return a placeholder response
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Media proxy not implemented yet"
            },
        )
    except Exception as e:
        import traceback

        tb = traceback.format_exc()
        print(f"Exception in proxy_media: {str(e)}")
        print(tb)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": f"Internal Server Error: {str(e)}"
            },
        )


# Initialize mock daily summaries
def mock_daily_summaries():
    """Generate mock daily summaries for testing"""
    return [
        {
            "_id": "mock1",
            "date": (datetime.now() - timedelta(days=1)).date().isoformat(),
            "group_sid": "all",
            "summary":
            "Team handled 15 customer inquiries. Key topics included product pricing, delivery times, and refund requests.",
            "keywords": ["pricing", "delivery", "refund"],
            "sentiment": "positive",
            "message_count": 42,
            "active_users": 8,
        },
        {
            "_id": "mock2",
            "date": (datetime.now() - timedelta(days=2)).date().isoformat(),
            "group_sid": "all",
            "summary":
            "Several new product inquiries were discussed. Customers requested information on product specifications and availability.",
            "keywords": ["product", "specifications", "availability"],
            "sentiment": "neutral",
            "message_count": 37,
            "active_users": 6,
        },
        {
            "_id": "mock3",
            "date": (datetime.now() - timedelta(days=3)).date().isoformat(),
            "group_sid": "all",
            "summary":
            "Customer reported issues with order processing. The team provided troubleshooting steps and escalated to technical support.",
            "keywords": ["order", "processing", "technical support"],
            "sentiment": "negative",
            "message_count": 28,
            "active_users": 5,
        },
    ]


# Initialize mock group analytics data
def mock_group_report(group_id: str, start_date: datetime, end_date: datetime):
    """Generate mock group analytics report for testing"""
    mock_group_name = f"Group {group_id[-4:]}" if len(
        group_id) > 4 else "Test Group"

    return {
        "group_id":
        group_id,
        "group_name":
        mock_group_name,
        "date_range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat()
        },
        "summary": {
            "totalMessages": 125,
            "totalParticipants": 8,
            "activeParticipants": 6,
            "messageTypes": {
                "text": 95,
                "media": 15,
                "document": 10,
                "voice": 5
            },
        },
        "totalMessages":
        125,
        "totalParticipants":
        8,
        "activeParticipants":
        6,
        "messageTypes": {
            "text": 95,
            "media": 15,
            "document": 10,
            "voice": 5
        },
        "topKeywords": [
            {
                "keyword": "meeting",
                "count": 25
            },
            {
                "keyword": "project",
                "count": 18
            },
            {
                "keyword": "deadline",
                "count": 15
            },
            {
                "keyword": "report",
                "count": 12
            },
            {
                "keyword": "client",
                "count": 10
            },
        ],
        "topParticipants": [
            {
                "_id": "whatsapp:+1234567890",
                "message_count": 45,
                "name": "Team Lead"
            },
            {
                "_id": "whatsapp:+1234567891",
                "message_count": 32,
                "name": "Team Member 1",
            },
            {
                "_id": "whatsapp:+1234567892",
                "message_count": 28,
                "name": "Team Member 2",
            },
        ],
        "messageFrequency": [
            {
                "date": (start_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                "count": 15,
            },
            {
                "date": (start_date + timedelta(days=2)).strftime("%Y-%m-%d"),
                "count": 22,
            },
            {
                "date": (start_date + timedelta(days=3)).strftime("%Y-%m-%d"),
                "count": 18,
            },
            {
                "date": (start_date + timedelta(days=4)).strftime("%Y-%m-%d"),
                "count": 25,
            },
            {
                "date": (start_date + timedelta(days=5)).strftime("%Y-%m-%d"),
                "count": 12,
            },
        ],
    }


@app.post("/api/summaries/generate", tags=["Summaries"])
async def generate_summaries(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    group_sid: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_admin_user)):
    """
    Manually generate summaries for the specified date range and group.
    If no dates are provided, generates for yesterday.
    If no group is provided, generates for all groups.
    """
    try:
        # Default to yesterday if no dates provided
        if not start_date:
            start_date = datetime.now() - timedelta(days=1)
            start_date = datetime.combine(start_date.date(),
                                          datetime.min.time())

        if not end_date:
            end_date = datetime.combine(start_date.date(), datetime.max.time())

        # Find groups to process
        query = {"is_active": True}
        if group_sid:
            query["group_sid"] = group_sid

        cursor = db.groups.find(query)
        groups = await cursor.to_list(length=100)

        if not groups:
            return {"message": "No matching active groups found"}

        results = []
        for group in groups:
            group_id = group["group_sid"]

            # Get messages for the time range
            msg_query = {
                "group_id": group_id,
                "timestamp": {
                    "$gte": start_date.timestamp(),
                    "$lte": end_date.timestamp()
                },
            }

            total_messages = await db.processed_messages.count_documents(
                msg_query)

            # Skip if no activity
            if total_messages == 0:
                results.append({
                    "group_id": group_id,
                    "date": start_date.date().isoformat(),
                    "skipped": True,
                    "reason": "No messages in time range"
                })
                continue

            # Generate summary
            messages_cursor = db.processed_messages.find(msg_query).sort(
                "timestamp", ASCENDING)
            messages = await messages_cursor.to_list(length=None)

            # Extract message content for summarization
            message_texts = []
            for msg in messages:
                if msg.get("body"):
                    sender_prefix = f"{msg.get('from_name', 'Unknown')}: " if msg.get(
                        "from_name") else ""
                    message_texts.append(f"{sender_prefix}{msg['body']}")

            if not message_texts:
                results.append({
                    "group_id": group_id,
                    "date": start_date.date().isoformat(),
                    "skipped": True,
                    "reason": "No message content to summarize"
                })
                continue

            # Generate summary text
            combined_text = "\n".join(message_texts)
            summary_text = await summarize_text(combined_text)

            # Create or update summary
            summary_doc = {
                "group_sid": group_id,
                "group_name": group.get("name", "Unknown Group"),
                "date": start_date.date().isoformat(),
                "message_count": total_messages,
                "summary": summary_text,
                "generated_at": datetime.now(),
            }

            # Store in daily_summaries collection
            result = await db.daily_summaries.update_one(
                {
                    "group_sid": group_id,
                    "date": start_date.date().isoformat()
                },
                {"$set": summary_doc},
                upsert=True,
            )

            results.append({
                "group_id": group_id,
                "date": start_date.date().isoformat(),
                "success": True,
                "summary": summary_text,
                "message_count": total_messages
            })

        return {
            "success": True,
            "date_range": {
                "start": start_date.date().isoformat(),
                "end": end_date.date().isoformat()
            },
            "results": results
        }

    except Exception as e:
        import traceback
        print(f"Error generating summaries: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system/audit-logs")
async def get_audit_logs(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    current_user: UserInDB = Depends(get_current_admin_user)
):
    skip = (page - 1) * limit
    logs_cursor = db.audit_logs.find().sort("timestamp", DESCENDING).skip(skip).limit(limit)
    logs = await logs_cursor.to_list(length=limit)
    total = await db.audit_logs.count_documents({})
    # Convert ObjectId to string for frontend
    for log in logs:
        if "_id" in log:
            log["_id"] = str(log["_id"])
    return {"logs": logs, "total": total}

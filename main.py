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
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Depends, status, Response, Query, Body, File, UploadFile, Path
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Optional
import time
from datetime import datetime, timedelta
from pydantic import BaseModel, EmailStr
from utils import process_message, get_processed_messages, transcribe_voice, summarize_text
from pymongo import MongoClient, ASCENDING, DESCENDING
from models import (
    GroupConfig, GroupCreate, GroupUpdate, FilterParams, 
    PaginationParams, GroupStats, MessageType, UserRole,
    ParticipantCreate, Participant, ParticipantRole,
    Message, MessageStatus
)
from models import UserOut
from pymongo.errors import OperationFailure, ConnectionFailure
from auth import (
    Token, UserCreate, UserInDB, get_password_hash, verify_password,
    create_access_token, get_current_user, get_current_active_user,
    get_current_admin_user, create_refresh_token, add_refresh_token, remove_refresh_token,
    ACCESS_TOKEN_EXPIRE_MINUTES, generate_token, send_verification_email
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
    contextual_reply_user_prompt
)
from bson import ObjectId
import re

# Set up logging to file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    handlers=[
        logging.FileHandler("backend.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Check required environment variables
required_env_vars = [
    "TWILIO_AUTH_TOKEN",
    "TWILIO_ACCOUNT_SID",
    "MONGODB_URI",
    "MONGODB_DB"
]

missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Twilio credentials from .env
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")

# Twilio WhatsApp sender (from) number from environment
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")

# MongoDB connection with retry logic
def get_mongo_client():
    retries = 10
    for i in range(retries):
        try:
            client = MongoClient(os.getenv("MONGODB_URI"), serverSelectionTimeoutMS=5000)
            # Try to connect
            client.server_info()
            print("Successfully connected to MongoDB")
            return client
        except errors.ServerSelectionTimeoutError as err:
            print(f"MongoDB connection failed: {err}, retrying in 5 seconds...")
            time.sleep(5)
    print("Could not connect to MongoDB after several retries. Continuing without DB connection.")
    return None

client = get_mongo_client()
db = client[os.getenv("MONGODB_DB")] if client else None

# Create text index for keyword search
try:
    # Create a compound text index on body and keywords fields
    db.processed_messages.create_index([
        ("body", "text"),
        ("keywords", "text")
    ], name="message_text_search")
    print("Successfully created text index for message search")
except OperationFailure as e:
    # If index already exists, this error will be raised
    if "already exists" not in str(e):
        print(f"Error creating text index: {str(e)}")
except Exception as e:
    print(f"Unexpected error creating text index: {str(e)}")

app = FastAPI()

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8080",
        "http://localhost:5173",  # Vite default port
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:5173",
        # Add your production domain here when deploying
        # "https://your-production-domain.com"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"]
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
    end: str    # e.g. '18:00'

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
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is temporarily unavailable. Please try again later.")

@app.get("/health")
async def health_check():
    try:
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")

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
                raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        else:
            print("Signature check bypassed for testing.")

        # Extract message data
        message_body = form_data.get("Body", None)
        if not message_body:
            print("[ERROR] No 'Body' field in form_data! Full form_data:", dict(form_data))
            raise HTTPException(status_code=400, detail="No message body provided")

        # Extract group information
        group_sid = form_data.get("GroupSid")
        if not group_sid:
            print("[WARNING] No GroupSid in message. This might be a direct message.")
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
            start = dt.strptime(wh['start'], "%H:%M").time()
            end = dt.strptime(wh['end'], "%H:%M").time()
            if not (start <= now <= end):
                auto_reply = {
                    "group_id": group_sid,
                    "client_number": form_data.get("From"),
                    "reason": "after_hours",
                    "reply_text": f"Our team is available from {start.strftime('%H:%M')} to {end.strftime('%H:%M')}. We will respond to your message during working hours.",
                    "timestamp": datetime.now()
                }
                db.auto_replies.insert_one(auto_reply)
                response = MessagingResponse()
                response.message(f"Thank you for your message. Our working hours are {start.strftime('%H:%M')} to {end.strftime('%H:%M')}. We will respond during business hours.")
                return str(response)
        # --- End working hours check ---
        if is_weekend or is_company_holiday:
            # Create auto-reply document
            auto_reply = {
                "group_id": group_sid,
                "client_number": form_data.get("From"),
                "reason": "holiday",
                "reply_text": "We are currently closed for the weekend/holiday. We will respond to your message during our next business day.",
                "timestamp": datetime.now()
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
            raw_payload=dict(form_data)
        ).dict()

        # Store message in MongoDB
        result = db.messages.insert_one(message_doc)
        print(f"Stored message with ID: {result.inserted_id}")

        # Determine message type
        if form_data.get("MediaUrl0"):
            content_type = form_data.get("MediaContentType0", "")
            if content_type.startswith("audio"):
                message_doc["message_type"] = MessageType.VOICE
            elif content_type.startswith("application") or content_type.startswith("document"):
                message_doc["message_type"] = MessageType.DOCUMENT
            else:
                message_doc["message_type"] = MessageType.MEDIA

            # Update message type in database
            db.messages.update_one(
                {"_id": result.inserted_id},
                {"$set": {"message_type": message_doc["message_type"]}}
            )

        # Queue for background processing
        background_tasks.add_task(process_message, message_doc)

        # --- Auto-create group and participant on new group message ---
        if group_sid != "direct_message":
            # Check if group exists
            group = db.groups.find_one({"group_sid": group_sid})
            if not group:
                group_doc = {
                    "group_sid": group_sid,
                    "name": f"WhatsApp Group {group_sid[-4:]}" if group_sid else "Unknown Group",
                    "description": None,
                    "is_active": True,
                    "keywords": [],
                    "notification_settings": {}
                }
                db.groups.insert_one(group_doc)
            # Check if participant exists
            sender_number = form_data.get("From")
            participant = db.group_participants.find_one({
                "group_id": group_sid,
                "phone_number": sender_number
            })
            if not participant:
                participant_doc = {
                    "group_id": group_sid,
                    "phone_number": sender_number,
                    "name": sender_number,  # Default to number; can be updated later
                    "role": "client",
                    "added_at": datetime.now().isoformat(),
                    "is_active": True
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
            {"$set": {"escalation.delivered": True}}
        )
        if result.modified_count > 0:
            print(f"Marked escalation {message_sid} as delivered.")
    return {"status": "ok"}

@app.post("/api/groups", response_model=GroupConfig)
async def create_group(
    group: GroupCreate,
    current_user: UserInDB = Depends(get_current_active_user)
):
    """Create a new WhatsApp group configuration."""
    ensure_db()
    try:
        group_config = GroupConfig(
            group_sid=f"GRP_{int(time.time())}",
            name=group.name,
            description=group.description,
            keywords=group.keywords or [],
            notification_settings=group.notification_settings or {}
        )
        
        db.groups.insert_one(group_config.dict())
        return group_config
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups", response_model=List[GroupConfig])
async def list_groups():
    ensure_db()
    try:
        groups = list(db.groups.find())
        return [GroupConfig(**group) for group in groups]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups/{group_sid}", response_model=GroupConfig)
async def get_group(group_sid: str):
    ensure_db()
    try:
        group = db.groups.find_one({"group_sid": group_sid})
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
        update_data = {k: v for k, v in group_update.dict().items() if v is not None}
        if not update_data:
            raise HTTPException(status_code=400, detail="No update data provided")
        result = db.groups.find_one_and_update(
            {"group_sid": group_sid},
            {"$set": update_data},
            return_document=True
        )
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
        total_messages = db.processed_messages.count_documents({"group_sid": group_sid})
        
        # Get message type distribution
        message_types = db.processed_messages.aggregate([
            {"$match": {"group_sid": group_sid}},
            {"$group": {"_id": "$type", "count": {"$sum": 1}}}
        ])
        message_type_dist = {doc["_id"]: doc["count"] for doc in message_types}
        
        # Get unique active users
        active_users = len(db.processed_messages.distinct("from", {"group_sid": group_sid}))
        
        # Get last activity
        last_message = db.processed_messages.find_one(
            {"group_sid": group_sid},
            sort=[("timestamp", DESCENDING)]
        )
        last_activity = datetime.fromtimestamp(last_message["timestamp"]) if last_message else None
        
        # Get keywords (from group config)
        group = db.groups.find_one({"group_sid": group_sid})
        keywords = group.get("keywords", []) if group else []
        
        return GroupStats(
            total_messages=total_messages,
            message_types=message_type_dist,
            keywords=keywords,
            active_users=active_users,
            last_activity=last_activity
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/messages/processed")
async def get_processed_messages_endpoint(
    filters: FilterParams = Depends(),
    pagination: PaginationParams = Depends()
):
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
            query.setdefault("timestamp", {})["$lte"] = filters.end_date.timestamp()
        if filters.keyword:
            query["$text"] = {"$search": filters.keyword}
        # Build sort
        sort = []
        if pagination.sort_by:
            sort_direction = ASCENDING if pagination.sort_order == "asc" else DESCENDING
            sort.append((pagination.sort_by, sort_direction))
        sort.append(("timestamp", DESCENDING))
        # Get total count
        total = db.processed_messages.count_documents(query)
        # Get paginated results
        skip = (pagination.page - 1) * pagination.page_size
        messages = list(db.processed_messages.find(query)
                       .sort(sort)
                       .skip(skip)
                       .limit(pagination.page_size))
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
            "messages": messages,
            "total": total,
            "page": pagination.page,
            "page_size": pagination.page_size,
            "total_pages": (total + pagination.page_size - 1) // pagination.page_size
        }
    except Exception as e:
        import traceback
        print("Error in get_processed_messages_endpoint:", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/messages/process_message")
async def process_message_alias(
    filters: FilterParams = Depends(),
    pagination: PaginationParams = Depends()
):
    ensure_db()
    try:
        return await get_processed_messages_endpoint(filters, pagination)
    except Exception as e:
        import traceback
        print("Error in process_message_alias:", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/summaries/daily")
async def get_daily_summaries(
    group_sid: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
):
    ensure_db()
    try:
        query = {}
        if group_sid:
            query["group_sid"] = group_sid
        if start_date:
            query["date"] = {"$gte": start_date.date()}
        if end_date:
            query.setdefault("date", {})["$lte"] = end_date.date()
        summaries = list(db.daily_summaries.find(query).sort("date", DESCENDING))
        return {"summaries": summaries}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/summary/search")
async def search_summaries(request: SearchRequest):
    ensure_db()
    try:
        if not request.keyword.strip():
            raise HTTPException(status_code=400, detail="Keyword cannot be empty")
            
        # Simulate a small delay
        time.sleep(0.8)
        
        # Mock search results
        results = [
            {
                "id": "1",
                "title": f"Sample result for {request.keyword}",
                "date": "2024-02-20",
                "summary": f"This is a sample summary containing the keyword {request.keyword}",
                "relevance_score": 0.95
            },
            {
                "id": "2",
                "title": f"Another result about {request.keyword}",
                "date": "2024-02-19",
                "summary": f"Here's another summary mentioning {request.keyword}",
                "relevance_score": 0.85
            }
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
        media_items = list(db.media.find({}, {"_id": 0}))
        return {"media_items": media_items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch media items: {str(e)}")

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
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")

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
        raise HTTPException(status_code=500, detail=f"Summarization failed: {str(e)}")

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
                "from": "media_migration"
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
    leader = db.group_participants.find_one({"group_id": group_id, "role": "leader", "is_active": True})
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
            body=notification
        )
        logger.info(f"Escalation notification sent to leader {leader_number} for group {group_id}")
    except Exception as e:
        logger.error(f"Failed to send escalation notification to leader: {str(e)}")

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

        client_messages = db.messages.find({
            "timestamp": {"$lt": one_hour_ago},
            "sender": {"$regex": "^whatsapp:"},
            "escalation.ignored": {"$ne": True}
        })

        for message in client_messages:
            team_reply = db.messages.find_one({
                "group_id": message["group_id"],
                "timestamp": {"$gt": message["timestamp"]},
                "sender": {"$regex": "^team:"}
            })

            if not team_reply:
                update_data = {
                    "escalation.ignored": True,
                    "escalation.notified_at": current_time
                }
                if message["timestamp"] < two_hours_ago:
                    update_data["escalation.auto_replied"] = True

                # Lookup leader's phone_number
                leader = db.group_participants.find_one({
                    "group_id": message["group_id"],
                    "role": "leader",
                    "is_active": True
                })
                alert_sid = None
                if leader:
                    leader_number = leader.get("phone_number")
                    try:
                        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                        # Find the conversation for this group (assume friendly_name == group_id)
                        conversations = twilio_client.conversations.conversations.list(friendly_name=message["group_id"])
                        if conversations:
                            conversation_sid = conversations[0].sid
                            notification = (
                                f"[Escalation] Client message missed by team.\n"
                                f"Message: {message['body']}\n"
                                f"From: {message['sender']}\n"
                                f"Time: {message['timestamp']}"
                            )
                            msg = twilio_client.conversations.conversations(conversation_sid).messages.create(
                                author="system",
                                body=notification
                            )
                            alert_sid = msg.sid
                            update_data["escalation.alert_sid"] = alert_sid
                        else:
                            print(f"No Twilio conversation found for group_id {message['group_id']}")
                    except Exception as twilio_err:
                        print(f"Twilio notification failed: {twilio_err}")
                        update_data["escalation.alert_sid"] = None
                else:
                    print(f"No leader found for group {message['group_id']} to notify.")
                    update_data["escalation.alert_sid"] = None

                db.messages.update_one(
                    {"_id": message["_id"]},
                    {"$set": update_data}
                )

                print(f"Escalated message {message['_id']} at {current_time}, alert_sid: {alert_sid}")

    except Exception as e:
        print(f"Error in message escalation job: {str(e)}")

@app.on_event("startup")
async def startup_event():
    """Initialize scheduler and other startup tasks"""
    # Start the scheduler
    scheduler.start()
    
    # Add the message escalation job
    scheduler.add_job(
        check_message_escalation,
        trigger=IntervalTrigger(minutes=15),
        id='message_escalation',
        replace_existing=True
    )
    
    # Setup other scheduled tasks
    setup_scheduled_tasks()

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup tasks on shutdown"""
    scheduler.shutdown()

# Monitoring Endpoints
@app.get("/api/monitor/tasks", response_model=Dict)
async def get_task_status(current_user: UserInDB = Depends(get_current_admin_user)):
    """Get status of all scheduled tasks (admin only)."""
    return monitor.get_task_status()

@app.get("/api/monitor/webhook", response_model=Dict)
async def get_webhook_status(current_user: UserInDB = Depends(get_current_admin_user)):
    """Get webhook monitoring status (admin only)."""
    return monitor.get_webhook_status()

# --- User Management Endpoints (Admin Only) ---
@app.get("/api/users", response_model=List[UserInDB], tags=["User Management"])
async def list_users(current_user: UserInDB = Depends(get_current_admin_user)):
    """List all users (admin only)."""
    users = list(db.users.find())
    return [UserInDB(**u) for u in users]

def serialize_user(user):
    # Accepts a dict or Pydantic model
    return {
        "id": user.get("username") if isinstance(user, dict) else getattr(user, "username", None),
        "username": user.get("username") if isinstance(user, dict) else getattr(user, "username", None),
        "email": user.get("email") if isinstance(user, dict) else getattr(user, "email", None),
        "full_name": user.get("full_name") if isinstance(user, dict) else getattr(user, "full_name", None),
        "role": user.get("role", "").upper() if isinstance(user, dict) else getattr(user, "role", "").upper(),
        "is_active": user.get("is_active") if isinstance(user, dict) else getattr(user, "is_active", None),
        "is_verified": user.get("is_verified") if isinstance(user, dict) else getattr(user, "is_verified", None),
        "avatar": user.get("avatar") if isinstance(user, dict) else getattr(user, "avatar", None),
        # Add other fields as needed
    }

@app.get("/api/users/{username}", response_model=UserOut, tags=["User Management"])
async def get_user_profile(username: str, current_user: UserInDB = Depends(get_current_active_user)):
    ensure_db()
    if not username or username == "undefined":
        raise HTTPException(status_code=400, detail="Username is required.")
    if current_user.username != username and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized to view this user")
    user = db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_data = serialize_user(user)
    return user_data

@app.put("/api/users/{username}")
async def update_user_profile(username: str, update: dict, current_user: UserInDB = Depends(get_current_active_user)):
    ensure_db()
    if not username or username == "undefined":
        raise HTTPException(status_code=400, detail="Username is required.")
    if current_user.username != username and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    allowed_fields = {"full_name", "avatar", "email"}
    update_fields = {k: v for k, v in update.items() if k in allowed_fields}
    if not update_fields:
        raise HTTPException(status_code=400, detail="No valid fields to update.")
    result = db.users.find_one_and_update(
        {"username": username},
        {"$set": update_fields},
        return_document=True
    )
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return {"success": True, "user": result}

@app.delete("/api/users/{username}", tags=["User Management"])
async def delete_user(username: str, current_user: UserInDB = Depends(get_current_admin_user)):
    """Delete a user (admin only)."""
    result = db.users.delete_one({"username": username})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "success", "message": "User deleted"}

# --- System Status & Analytics Endpoints ---
@app.get("/api/system/status", tags=["System"])
async def system_status(current_user: UserInDB = Depends(get_current_admin_user)):
    """Get system status and basic analytics (admin only)."""
    return {
        "users": db.users.count_documents({}),
        "groups": db.groups.count_documents({}),
        "messages": db.processed_messages.count_documents({}),
        "active_groups": db.groups.count_documents({"is_active": True}),
        "inactive_groups": db.groups.count_documents({"is_active": False}),
        "daily_summaries": db.daily_summaries.count_documents({})
    }

@app.get("/api/analytics/group/{group_sid}", tags=["Analytics"])
async def group_analytics(group_sid: str):
    total_msgs = db.processed_messages.count_documents({"group_sid": group_sid})
    users = db.processed_messages.distinct("from", {"group_sid": group_sid})
    last_msg = db.processed_messages.find_one({"group_sid": group_sid}, sort=[("timestamp", DESCENDING)])
    return {
        "group_sid": group_sid,
        "total_messages": total_msgs,
        "unique_users": len(users),
        "last_activity": datetime.fromtimestamp(last_msg["timestamp"]) if last_msg else None
    }

@app.get("/api/analytics/messages", tags=["Analytics"])
async def message_analytics(
    group_sid: Optional[str] = None,
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    message_type: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_admin_user)
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
    page_size: int = 20
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
    messages = list(db.processed_messages.find(query).sort("timestamp", DESCENDING).skip(skip).limit(page_size))
    total = db.processed_messages.count_documents(query)
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
    return {
        "messages": messages,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size
    }

@app.post("/api/token")
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    remember_me: bool = False
):
    import os
    from fastapi.responses import JSONResponse
    print("/api/token called")
    print("form_data.username:", form_data.username)
    print("Environment variables:")
    print("MONGODB_URI:", os.getenv("MONGODB_URI"))
    print("MONGODB_DB:", os.getenv("MONGODB_DB"))
    print("JWT_SECRET_KEY:", os.getenv("JWT_SECRET_KEY"))
    
    try:
        # Try to find user by username or email
        user = db.users.find_one({
            "$or": [
                {"username": form_data.username},
                {"email": form_data.username}
            ]
        })
        print("User found:", user)
        
        if not user:
            print("No user found for username/email")
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid credentials"}
            )
            
        user_obj = UserInDB(**user)
        print("UserInDB loaded")
        
        # Verify password
        if not verify_password(form_data.password, user_obj.hashed_password):
            print("Password verification failed")
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid credentials"}
            )
        print("Password verified")
        
        # Check if user is active
        if not user_obj.is_active:
            print("User is not active")
            return JSONResponse(
                status_code=401,
                content={"error": "Inactive user"}
            )
            
        # Check if email is verified
        if not user_obj.is_verified:
            print("User email not verified")
            return JSONResponse(
                status_code=401,
                content={"error": "Please confirm your email before logging in."}
            )
        print("User is active and verified")
        
        # Create access token
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        if remember_me:
            access_token_expires = timedelta(days=7)  # Longer expiry for "remember me"
        access_token = create_access_token(
            data={"sub": user_obj.username, "role": user_obj.role},
            expires_delta=access_token_expires
        )
        print("Access token created")
        
        # Create refresh token
        refresh_token = create_refresh_token(user_obj.username)
        print("Refresh token created")
        
        # Store refresh token
        await add_refresh_token(user_obj.username, refresh_token)
        print("Refresh token stored")
        
        user_data = serialize_user(user)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "username": user_obj.username,
            "token_type": "bearer",
            "user": user_data
        }
        
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Exception in login_for_access_token: {tb}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": f"Internal Server Error: {str(e)}"}
        )

@app.get("/api/auth/verify", response_model=UserOut)
async def verify_auth(current_user: UserInDB = Depends(get_current_active_user)):
    """
    Verify if the current user is authenticated.
    Returns user information if authenticated.
    """
    return serialize_user(current_user)

@app.post("/api/register")
async def register_user(
    user: UserCreate = Body(...)
):
    try:
        ensure_db()
        # Check for duplicate username or email
        if db.users.find_one({"$or": [{"username": user.username}, {"email": user.email}]}):
            return JSONResponse(
                status_code=409,
                content={"success": False, "error": "Username or email already exists"}
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
            "refresh_tokens": []
        }
        db.users.insert_one(user_doc)
        # Send confirmation email
        await send_verification_email(user.email, confirmation_token)
        return JSONResponse(
            status_code=201,
            content={"success": True, "message": "Registration successful. Please check your email to confirm your account.", "user": serialize_user(user_doc)}
        )
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(e)}
        )

@app.get("/api/confirm-email")
async def confirm_email(token: str):
    try:
        user = db.users.find_one({"verification_token": token})
        if not user:
            return JSONResponse(status_code=400, content={"success": False, "error": "Invalid or expired confirmation token."})
        db.users.update_one({"_id": user["_id"]}, {"$set": {"is_verified": True, "verification_token": None}})
        # Optionally, redirect to a confirmation page
        # from fastapi.responses import RedirectResponse
        # return RedirectResponse(url="https://yourdomain.com/email-confirmed")
        return JSONResponse(status_code=200, content={"success": True, "message": "Email confirmed! You can now log in."})
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)}) 

@app.get("/api/media/proxy")
async def proxy_media(url: str):
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    r = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), stream=True)
    if r.status_code != 200:
        return Response(status_code=r.status_code)
    return StreamingResponse(r.raw, media_type=r.headers.get("Content-Type")) 

@app.post("/api/groups/{group_id}/participants", tags=["Group Management"])
async def add_group_participant_protected(
    group_id: str,
    participant: ParticipantAddRequest,
    current_user: UserInDB = Depends(get_current_active_user)
):
    ensure_db()
    # Check if current user is admin
    is_admin = current_user.role == "admin"
    # Or if current user is a leader in this group
    is_leader = db.group_participants.find_one({
        "group_id": group_id,
        "$or": [
            {"user_id": current_user.username},
            {"phone_number": current_user.username}
        ],
        "role": "leader",
        "is_active": True
    })
    if not (is_admin or is_leader):
        raise HTTPException(status_code=403, detail="Only admin or group leader can add participants.")
    # Validate role
    if participant.role not in ["team", "client", "leader"]:
        raise HTTPException(status_code=400, detail="Role must be 'team', 'client', or 'leader'")
    # Check if participant already exists
    existing = db.group_participants.find_one({
        "group_id": group_id,
        "$or": [
            {"user_id": participant.user_id},
            {"phone_number": participant.user_id}
        ]
    })
    if existing:
        raise HTTPException(status_code=400, detail="Participant already exists in this group")
    # Insert participant
    participant_doc = {
        "group_id": group_id,
        "user_id": participant.user_id,
        "phone_number": participant.user_id,  # for compatibility
        "name": participant.name,
        "role": participant.role,
        "added_at": datetime.now().isoformat(),
        "is_active": True
    }
    db.group_participants.insert_one(participant_doc)
    return {"success": True, "message": f"Participant {participant.name} added as {participant.role}."}

@app.get("/api/groups/{group_id}/participants", response_model=List[Participant])
async def get_group_participants(
    group_id: str,
    current_user: UserInDB = Depends(get_current_active_user)
):
    """
    Get all participants for a specific group.
    """
    try:
        ensure_db()
        # Verify group exists
        group = db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        # Get all active participants
        participants = list(db.group_participants.find({
            "group_id": group_id,
            "is_active": True
        }))
        # Ensure 'added_at' is always a valid ISO string
        for p in participants:
            if "added_at" not in p or not p["added_at"]:
                p["added_at"] = datetime.now().isoformat()
            elif isinstance(p["added_at"], datetime):
                p["added_at"] = p["added_at"].isoformat()
            # Map group role to display role
            if p.get("role") == "leader":
                p["display_role"] = "Team Leader"
            elif p.get("role") == "team":
                p["display_role"] = "Team Member"
            elif p.get("role") == "client":
                p["display_role"] = "Client"
            else:
                p["display_role"] = p.get("role", "Unknown")
        return [Participant(**p) for p in participants]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/groups/{group_id}/participants/{phone_number}")
async def remove_group_participant(
    group_id: str,
    phone_number: str,
    current_user: UserInDB = Depends(get_current_active_user)
):
    """
    Remove a participant from a group (soft delete).
    """
    try:
        # Verify group exists
        group = db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        # Find and update participant
        result = db.group_participants.update_one(
            {
                "group_id": group_id,
                "phone_number": phone_number
            },
            {
                "$set": {"is_active": False}
            }
        )

        if result.modified_count == 0:
            raise HTTPException(
                status_code=404,
                detail="Participant not found in this group"
            )

        return {"status": "success", "message": "Participant removed from group"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Initialize Groq client
groq_client = groq.Client(api_key=os.getenv("GROQ_API_KEY"))

async def generate_contextual_reply(group_id: str, num_messages: int = 5) -> Dict:
    """
    Generate a contextual reply based on recent messages in a group.
    """
    try:
        # Fetch recent messages
        recent_messages = list(db.messages.find(
            {"group_id": group_id}
        ).sort("timestamp", -1).limit(num_messages))
        if not recent_messages:
            return None
        # Format messages for context
        context = "\n".join([
            f"{msg['sender']}: {msg['body']}"
            for msg in reversed(recent_messages)
        ])
        # Use prompt from ai_prompts.py
        prompt = contextual_reply_user_prompt.format(context=context)
        try:
            # Call Groq API
            completion = groq_client.chat.completions.create(
                model="mixtral-8x7b-32768",
                messages=[
                    {"role": "system", "content": contextual_reply_system_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=150
            )
            reply_text = completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"Error generating contextual reply: {str(e)}")
            reply_text = ""
        if not reply_text:
            # Fallback auto-reply
            fallback_reply = {
                "group_id": group_id,
                "client_number": recent_messages[0]["sender"],
                "reason": "contextual",
                "reply_text": "We'll get back to you shortly.",
                "timestamp": datetime.now(),
                "generated_by": "fallback",
                "reviewed": False,
                "context_messages": [str(msg["_id"]) for msg in recent_messages]
            }
            result = db.auto_replies.insert_one(fallback_reply)
            fallback_reply["_id"] = result.inserted_id
            return fallback_reply
        # Create auto-reply document
        auto_reply = {
            "group_id": group_id,
            "client_number": recent_messages[0]["sender"],
            "reason": "contextual",
            "reply_text": reply_text,
            "timestamp": datetime.now(),
            "generated_by": "groq",
            "reviewed": True,
            "context_messages": [str(msg["_id"]) for msg in recent_messages]
        }
        # Store in auto_replies collection
        result = db.auto_replies.insert_one(auto_reply)
        auto_reply["_id"] = result.inserted_id
        return auto_reply
    except Exception as e:
        print(f"Error generating contextual reply: {str(e)}")
        return None

@app.post("/api/groups/{group_id}/auto-reply")
async def generate_auto_reply(
    group_id: str,
    num_messages: int = Query(5, ge=1, le=10),
    current_user: UserInDB = Depends(get_current_active_user)
):
    """
    Generate and store an auto-reply for a group based on recent messages.
    """
    try:
        # Verify group exists
        group = db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
            
        # Generate reply
        auto_reply = await generate_contextual_reply(group_id, num_messages)
        
        if not auto_reply:
            raise HTTPException(
                status_code=400,
                detail="No messages found to generate context from"
            )
            
        return auto_reply
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups/{group_id}/auto-replies")
async def get_auto_replies(
    group_id: str,
    skip: int = 0,
    limit: int = 10,
    current_user: UserInDB = Depends(get_current_active_user)
):
    """
    Get auto-replies for a group.
    """
    try:
        # Verify group exists
        group = db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
            
        # Get auto-replies
        auto_replies = list(db.auto_replies.find(
            {"group_id": group_id}
        ).sort("timestamp", -1).skip(skip).limit(limit))
        
        # Convert ObjectId to string for JSON serialization
        for reply in auto_replies:
            reply["_id"] = str(reply["_id"])
            
        return {
            "auto_replies": auto_replies,
            "total": db.auto_replies.count_documents({"group_id": group_id})
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups/{group_id}/messages")
async def get_group_messages(
    group_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    message_type: Optional[MessageType] = None,
    current_user: UserInDB = Depends(get_current_active_user)
):
    """
    Get messages for a specific group with pagination and filtering.
    """
    try:
        # Verify group exists
        group = db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        # Build query
        query = {"group_id": group_id}
        if start_date:
            query["timestamp"] = {"$gte": start_date}
        if end_date:
            query.setdefault("timestamp", {})["$lte"] = end_date
        if message_type:
            query["message_type"] = message_type

        # Get messages with pagination
        messages = list(db.messages.find(query)
                       .sort("timestamp", -1)
                       .skip(skip)
                       .limit(limit))

        # Convert ObjectId to string for JSON serialization
        for msg in messages:
            msg["_id"] = str(msg["_id"])

        # Get total count for pagination
        total = db.messages.count_documents(query)

        # Ensure body/content is present for /api/groups/{group_id}/messages
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
            "messages": messages,
            "total": total,
            "page": (skip // limit) + 1,
            "page_size": limit,
            "total_pages": (total + limit - 1) // limit
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups/{group_id}/escalations")
async def get_group_escalations(
    group_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    status: Optional[str] = None,  # 'ignored', 'auto_replied', etc.
    current_user: UserInDB = Depends(get_current_active_user)
):
    """
    Get escalated messages for a specific group with pagination and filtering.
    """
    try:
        # Verify group exists
        group = db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        # Build query
        query = {
            "group_id": group_id,
            "escalation": {"$exists": True}
        }
        if start_date:
            query["timestamp"] = {"$gte": start_date}
        if end_date:
            query.setdefault("timestamp", {})["$lte"] = end_date
        if status:
            if status == "ignored":
                query["escalation.ignored"] = True
            elif status == "auto_replied":
                query["escalation.auto_replied"] = True

        # Get messages with pagination
        messages = list(db.messages.find(query)
                       .sort("timestamp", -1)
                       .skip(skip)
                       .limit(limit))

        # Convert ObjectId to string for JSON serialization
        for msg in messages:
            msg["_id"] = str(msg["_id"])

        # Get total count for pagination
        total = db.messages.count_documents(query)

        return {
            "escalations": messages,
            "total": total,
            "page": (skip // limit) + 1,
            "page_size": limit,
            "total_pages": (total + limit - 1) // limit
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups/{group_id}/auto_replies")
async def get_group_auto_replies(
    group_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    reason: Optional[str] = None,  # 'holiday', 'contextual', etc.
    reviewed: Optional[bool] = None,
    current_user: UserInDB = Depends(get_current_active_user)
):
    """
    Get auto-replies for a specific group with pagination and filtering.
    """
    try:
        # Verify group exists
        group = db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        # Build query
        query = {"group_id": group_id}
        if start_date:
            query["timestamp"] = {"$gte": start_date}
        if end_date:
            query.setdefault("timestamp", {})["$lte"] = end_date
        if reason:
            query["reason"] = reason
        if reviewed is not None:
            query["reviewed"] = reviewed

        # Get auto-replies with pagination
        auto_replies = list(db.auto_replies.find(query)
                          .sort("timestamp", -1)
                          .skip(skip)
                          .limit(limit))

        # Convert ObjectId to string for JSON serialization
        for reply in auto_replies:
            reply["_id"] = str(reply["_id"])

        # Get total count for pagination
        total = db.auto_replies.count_documents(query)

        return {
            "auto_replies": auto_replies,
            "total": total,
            "page": (skip // limit) + 1,
            "page_size": limit,
            "total_pages": (total + limit - 1) // limit
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/refresh-token")
async def refresh_token_endpoint(payload: dict = Body(...)):
    """
    Accepts a refresh_token and returns a new access_token and refresh_token if valid.
    """
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        return JSONResponse(status_code=400, content={"error": "refresh_token is required"})
    try:
        from auth import SECRET_KEY, ALGORITHM, db
        payload = jwt.decode(refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Invalid token")
        user = db.users.find_one({"username": username})
        if not user or refresh_token not in user.get("refresh_tokens", []):
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
        # Issue new tokens
        access_token = create_access_token(
            data={"sub": username, "role": user["role"]},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        new_refresh_token = create_refresh_token(username)
        await add_refresh_token(username, new_refresh_token)
        await remove_refresh_token(username, refresh_token)
        return {
            "access_token": access_token,
            "refresh_token": new_refresh_token
        }
    except JWTError:
        return JSONResponse(status_code=401, content={"error": "Invalid or expired refresh token"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)}) 

@app.get("/api/whatsapp/groups-and-contacts")
async def list_groups_and_contacts(current_user: UserInDB = Depends(get_current_active_user)):
    """
    List all WhatsApp groups the bot is a member of and all direct contacts who have messaged the bot.
    For each group: return group name, group SID, participant list, and latest message.
    For each direct contact: return WhatsApp number, name (if available), and latest message.
    """
    # Groups
    groups = list(db.groups.find())
    group_infos = []
    for group in groups:
        group_sid = group["group_sid"]
        participants = list(db.group_participants.find({"group_id": group_sid, "is_active": True}))
        latest_msg = db.messages.find_one({"group_id": group_sid}, sort=[("timestamp", -1)])
        # Ensure body/content is present for latest_msg
        latest_body = None
        if latest_msg:
            if "body" in latest_msg:
                latest_body = latest_msg["body"]
            elif "content" in latest_msg:
                latest_body = latest_msg["content"]
            elif "text" in latest_msg:
                latest_body = latest_msg["text"]
            elif "transcription" in latest_msg:
                latest_body = latest_msg["transcription"]
            elif "notes" in latest_msg:
                latest_body = latest_msg["notes"]
        group_infos.append({
            "group_sid": group_sid,
            "group_name": group.get("name"),
            "participants": [
                {
                    "name": p.get("name"),
                    "phone_number": p.get("phone_number"),
                    "role": p.get("role")
                } for p in participants
            ],
            "latest_message": {
                "body": latest_body,
                "timestamp": latest_msg["timestamp"] if latest_msg else None
            }
        })
    # Direct contacts
    direct_msgs = db.messages.find({"group_id": "direct_message"})
    contacts = {}
    for msg in direct_msgs:
        sender = msg["sender"]
        # Ensure body/content is present for contact latest_message
        contact_body = None
        if "body" in msg:
            contact_body = msg["body"]
        elif "content" in msg:
            contact_body = msg["content"]
        elif "text" in msg:
            contact_body = msg["text"]
        elif "transcription" in msg:
            contact_body = msg["transcription"]
        elif "notes" in msg:
            contact_body = msg["notes"]
        if sender not in contacts or msg["timestamp"] > contacts[sender]["timestamp"]:
            contacts[sender] = {
                "whatsapp_number": sender,
                "name": msg.get("sender_name"),
                "latest_message": {
                    "body": contact_body,
                    "timestamp": msg["timestamp"]
                }
            }
    return {
        "groups": group_infos,
        "contacts": list(contacts.values())
    }

@app.get("/api/whatsapp/group/{group_sid}/history")
async def get_group_history(group_sid: str, skip: int = 0, limit: int = 100, current_user: UserInDB = Depends(get_current_active_user)):
    """
    Fetch full conversation history for a group.
    """
    messages = list(db.messages.find({"group_id": group_sid}).sort("timestamp", 1).skip(skip).limit(limit))
    for msg in messages:
        msg["_id"] = str(msg["_id"])
    return {"messages": messages}

@app.get("/api/whatsapp/contact/{phone_number}/history")
async def get_contact_history(phone_number: str, skip: int = 0, limit: int = 100, current_user: UserInDB = Depends(get_current_active_user)):
    """
    Fetch full conversation history for a direct contact.
    """
    messages = list(db.messages.find({"group_id": "direct_message", "sender": phone_number}).sort("timestamp", 1).skip(skip).limit(limit))
    for msg in messages:
        msg["_id"] = str(msg["_id"])
    return {"messages": messages} 

class AdminUserCreate(BaseModel):
    full_name: str
    username: str
    email: EmailStr
    password: str
    role: str  # 'user' or 'admin'

@app.post("/api/users", tags=["User Management"])
async def admin_create_user(
    user: AdminUserCreate,
    current_user: UserInDB = Depends(get_current_admin_user)
):
    ensure_db()
    if user.role not in ["user", "admin"]:
        raise HTTPException(status_code=400, detail="Role must be 'user' or 'admin'")
    if db.users.find_one({"$or": [{"username": user.username}, {"email": user.email}]}):
        raise HTTPException(status_code=409, detail="Username or email already exists")
    hashed_password = get_password_hash(user.password)
    confirmation_token = generate_token(48)
    user_doc = {
        "username": user.username,
        "email": user.email,
        "hashed_password": hashed_password,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": True,
        "is_verified": False,
        "verification_token": confirmation_token,
        "refresh_tokens": []
    }
    db.users.insert_one(user_doc)
    await send_verification_email(user.email, confirmation_token)
    return {"success": True, "message": f"User {user.username} created with role {user.role}."} 

@app.put("/api/groups/{group_id}/participants/{user_id}/role", tags=["Group Management"])
async def update_participant_role(
    group_id: str,
    user_id: str,
    role_update: ParticipantRoleUpdateRequest,
    current_user: UserInDB = Depends(get_current_active_user)
):
    ensure_db()
    # Check if current user is admin
    is_admin = current_user.role == "admin"
    # Or if current user is a leader in this group
    is_leader = db.group_participants.find_one({
        "group_id": group_id,
        "$or": [
            {"user_id": current_user.username},
            {"phone_number": current_user.username}
        ],
        "role": "leader",
        "is_active": True
    })
    if not (is_admin or is_leader):
        raise HTTPException(status_code=403, detail="Only admin or group leader can change participant roles.")
    if role_update.new_role not in ["team", "client", "leader"]:
        raise HTTPException(status_code=400, detail="Role must be 'team', 'client', or 'leader'")
    result = db.group_participants.update_one(
        {"group_id": group_id, "$or": [{"user_id": user_id}, {"phone_number": user_id}]},
        {"$set": {"role": role_update.new_role}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Participant not found")
    return {"success": True, "message": f"Role updated to {role_update.new_role}"} 

@app.post("/api/users/{username}/avatar")
async def upload_user_avatar(username: str, avatar: UploadFile = File(...), current_user: UserInDB = Depends(get_current_active_user)):
    ensure_db()
    if not username or username == "undefined":
        raise HTTPException(status_code=400, detail="Username is required.")
    if current_user.username != username and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    upload_dir = "uploads/avatars"
    os.makedirs(upload_dir, exist_ok=True)
    ext = avatar.filename.split('.')[-1]
    avatar_filename = f"{username}.{ext}"
    avatar_path = os.path.join(upload_dir, avatar_filename)
    with open(avatar_path, "wb") as buffer:
        shutil.copyfileobj(avatar.file, buffer)
    avatar_url = f"/uploads/avatars/{avatar_filename}"
    db.users.update_one({"username": username}, {"$set": {"avatar": avatar_url}})
    return {"avatar": avatar_url}

# Method Not Allowed handler for unsupported methods on /api/users/{username} and /api/users/{username}/avatar
@app.api_route("/api/users/{username}", methods=["POST", "PATCH", "DELETE"])
@app.api_route("/api/users/{username}/avatar", methods=["PUT", "PATCH", "DELETE"])
async def method_not_allowed(request: Request, username: str):
    return JSONResponse(status_code=405, content={"detail": "Method Not Allowed"}) 

@app.get("/api/company_holidays")
async def get_company_holidays(current_user: UserInDB = Depends(get_current_active_user)):
    ensure_db()
    holidays = list(db.company_holidays.find())
    def format_date(iso_date):
        try:
            d = datetime.strptime(iso_date, "%Y-%m-%d")
            return d.strftime("%d/%m/%Y")
        except Exception:
            return iso_date
    return [{
        "id": str(h["_id"]),
        "date": format_date(h["date"]),
        "description": h.get("description", "")
    } for h in holidays]

@app.post("/api/company_holidays")
async def add_company_holiday(
    holiday: HolidayCreateRequest,
    current_user: UserInDB = Depends(get_current_admin_user)
):
    ensure_db()
    # Validate date format (accepts YYYY-MM-DD or dd/mm/yyyy)
    iso_date = holiday.date
    if re.match(r"^\d{2}/\d{2}/\d{4}$", iso_date):
        try:
            iso_date = datetime.strptime(iso_date, "%d/%m/%Y").strftime("%Y-%m-%d")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid date format.")
    elif not re.match(r"^\d{4}-\d{2}-\d{2}$", iso_date):
        raise HTTPException(status_code=400, detail="Date must be in YYYY-MM-DD or dd/mm/yyyy format.")
    # Prevent duplicates
    if db.company_holidays.find_one({"date": iso_date}):
        raise HTTPException(status_code=400, detail="Holiday already exists.")
    result = db.company_holidays.insert_one({"date": iso_date, "description": holiday.description or ""})
    return {"id": str(result.inserted_id), "date": iso_date, "description": holiday.description or ""}

@app.delete("/api/company_holidays/{id}")
async def delete_company_holiday(id: str, current_user: UserInDB = Depends(get_current_admin_user)):
    ensure_db()
    try:
        result = db.company_holidays.delete_one({"_id": ObjectId(id)})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Holiday not found.")
        return Response(status_code=204)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid holiday id.") 

@app.get("/api/company_working_hours")
async def get_company_working_hours(current_user: UserInDB = Depends(get_current_active_user)):
    ensure_db()
    wh = db.company_working_hours.find_one()
    if wh:
        return {"start": wh["start"], "end": wh["end"]}
    return {"start": "09:00", "end": "18:00"}  # Default

@app.post("/api/company_working_hours")
async def set_company_working_hours(
    req: WorkingHoursRequest,
    current_user: UserInDB = Depends(get_current_admin_user)
):
    ensure_db()
    # Validate format
    if not re.match(r"^\d{2}:\d{2}$", req.start) or not re.match(r"^\d{2}:\d{2}$", req.end):
        raise HTTPException(status_code=400, detail="Time must be in HH:MM format.")
    db.company_working_hours.delete_many({})  # Only one config
    db.company_working_hours.insert_one({"start": req.start, "end": req.end})
    return {"start": req.start, "end": req.end} 

@app.patch("/api/company_holidays/{id}")
async def update_company_holiday_description(
    id: str = Path(...),
    payload: dict = Body(...),
    current_user: UserInDB = Depends(get_current_admin_user)
):
    ensure_db()
    desc = payload.get("description", "")
    result = db.company_holidays.update_one({"_id": ObjectId(id)}, {"$set": {"description": desc}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Holiday not found.")
    return {"id": id, "description": desc} 
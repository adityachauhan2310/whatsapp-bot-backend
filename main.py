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
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Depends, status, Response, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Optional
import time
from datetime import datetime, timedelta
from pydantic import BaseModel
from utils import process_message, get_processed_messages, transcribe_voice, summarize_text
from pymongo import MongoClient, ASCENDING, DESCENDING
from models import (
    GroupConfig, GroupCreate, GroupUpdate, FilterParams, 
    PaginationParams, GroupStats, MessageType, UserRole,
    ParticipantCreate, Participant, ParticipantRole,
    Message, MessageStatus
)
from pymongo.errors import OperationFailure, ConnectionFailure
from auth import (
    Token, UserCreate, UserInDB, get_password_hash, verify_password,
    create_access_token, get_current_user, get_current_active_user,
    get_current_admin_user, create_refresh_token, add_refresh_token,
    ACCESS_TOKEN_EXPIRE_MINUTES, generate_token, send_verification_email
)
from scheduler import monitor, setup_scheduled_tasks
from fastapi.security import OAuth2PasswordRequestForm
from pathlib import Path
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import holidays
import groq
import logging

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

# MongoDB connection with error handling
try:
    client = MongoClient(os.getenv("MONGODB_URI"), serverSelectionTimeoutMS=5000)
    # Test the connection
    client.server_info()
    db = client[os.getenv("MONGODB_DB")]
    print("Successfully connected to MongoDB")
except ConnectionFailure as e:
    print(f"Failed to connect to MongoDB: {str(e)}")
    raise
except Exception as e:
    print(f"Unexpected error connecting to MongoDB: {str(e)}")
    raise

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

# Background task to process incoming messages
async def process_incoming_message(message_data: Dict):
    # Simulate processing delay
    time.sleep(1)
    # Store message in MongoDB
    db.messages.insert_one(message_data)
    print(f"Processed and stored message: {message_data}")

@app.get("/health")
async def health_check():
    try:
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")

@app.post("/webhook/twilio")
async def twilio_webhook(request: Request, background_tasks: BackgroundTasks):
    """Twilio webhook endpoint for processing incoming WhatsApp messages."""
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

        # Check if today is a holiday or weekend
        today = datetime.now().date()
        us_holidays = holidays.US()  # You can change this to your country's holidays
        
        if today.weekday() >= 5 or today in us_holidays:  # 5 = Saturday, 6 = Sunday
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

        # Return TwiML response
        response = MessagingResponse()
        return str(response)

    except Exception as e:
        import traceback
        print("Webhook error:", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/groups", response_model=GroupConfig)
async def create_group(
    group: GroupCreate,
    current_user: UserInDB = Depends(get_current_active_user)
):
    """Create a new WhatsApp group configuration."""
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
    try:
        groups = list(db.groups.find())
        return [GroupConfig(**group) for group in groups]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups/{group_sid}", response_model=GroupConfig)
async def get_group(group_sid: str):
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

async def check_message_escalation():
    """
    Check for messages that need escalation:
    1. After 1 hour: Mark as ignored and set notification time
    2. After 2 hours: Mark as auto-replied
    """
    try:
        current_time = datetime.now()
        one_hour_ago = current_time - timedelta(hours=1)
        two_hours_ago = current_time - timedelta(hours=2)

        # Find client messages older than 1 hour with no team reply
        client_messages = db.messages.find({
            "timestamp": {"$lt": one_hour_ago},
            "sender": {"$regex": "^whatsapp:"},  # Client messages
            "escalation.ignored": {"$ne": True}  # Not already marked as ignored
        })

        for message in client_messages:
            # Check if there's a team reply after this message
            team_reply = db.messages.find_one({
                "group_id": message["group_id"],
                "timestamp": {"$gt": message["timestamp"]},
                "sender": {"$regex": "^team:"}  # Team messages
            })

            if not team_reply:
                # Update escalation status
                update_data = {
                    "escalation.ignored": True,
                    "escalation.notified_at": current_time
                }

                # If message is older than 2 hours, mark as auto-replied
                if message["timestamp"] < two_hours_ago:
                    update_data["escalation.auto_replied"] = True

                db.messages.update_one(
                    {"_id": message["_id"]},
                    {"$set": update_data}
                )

                print(f"Escalated message {message['_id']} at {current_time}")

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

@app.get("/api/users/{username}", response_model=UserInDB, tags=["User Management"])
async def get_user(username: str, current_user: UserInDB = Depends(get_current_active_user)):
    # Only allow users to fetch their own info, or admins to fetch anyone's
    if current_user.username != username and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not authorized to view this user")
    try:
        user = db.users.find_one({"username": username})
        if not user:
            return JSONResponse(status_code=404, content={"error": "User not found"})
        return JSONResponse(status_code=200, content=UserInDB(**user).dict())
    except Exception:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

@app.put("/api/users/{username}", response_model=UserInDB, tags=["User Management"])
async def update_user(username: str, update: dict, current_user: UserInDB = Depends(get_current_admin_user)):
    """Update user info (admin only)."""
    result = db.users.find_one_and_update({"username": username}, {"$set": update}, return_document=True)
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return UserInDB(**result)

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
    return {
        "messages": messages,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size
    }

@app.post("/api/token", response_model=Token)
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
            
        user = UserInDB(**user)
        print("UserInDB loaded")
        
        # Verify password
        if not verify_password(form_data.password, user.hashed_password):
            print("Password verification failed")
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid credentials"}
            )
        print("Password verified")
        
        # Check if user is active
        if not user.is_active:
            print("User is not active")
            return JSONResponse(
                status_code=401,
                content={"error": "Inactive user"}
            )
            
        # Check if email is verified
        if not user.is_verified:
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
            data={"sub": user.username, "role": user.role},
            expires_delta=access_token_expires
        )
        print("Access token created")
        
        # Create refresh token
        refresh_token = create_refresh_token(user.username)
        print("Refresh token created")
        
        # Store refresh token
        await add_refresh_token(user.username, refresh_token)
        print("Refresh token stored")
        
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "username": user.username,
            "token_type": "bearer"
        }
        
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Exception in login_for_access_token: {tb}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": f"Internal Server Error: {str(e)}"}
        )

@app.get("/api/auth/verify")
async def verify_auth(current_user: UserInDB = Depends(get_current_active_user)):
    """
    Verify if the current user is authenticated.
    Returns user information if authenticated.
    """
    return {
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role,
        "is_active": current_user.is_active,
        "is_verified": current_user.is_verified
    }

@app.post("/api/register")
async def register_user(
    user: UserCreate = Body(...)
):
    try:
        # Check for duplicate username or email
        if db.users.find_one({"$or": [{"username": user.username}, {"email": user.email}]}):
            return JSONResponse(
                status_code=409,
                content={"success": False, "error": "Username or email already exists"}
            )
        # Hash the password
        hashed_password = get_password_hash(user.password)
        # Default role to 'user' if not provided
        role = user.role if user.role else UserRole.USER
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
            content={"success": True, "message": "Registration successful. Please check your email to confirm your account."}
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

@app.post("/api/groups/{group_id}/participants", response_model=Participant)
async def add_group_participant(
    group_id: str,
    participant: ParticipantCreate,
    current_user: UserInDB = Depends(get_current_active_user)
):
    """
    Add a new participant to a WhatsApp group.
    Only active users can add participants.
    """
    try:
        # Verify group exists
        group = db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        # Check if participant already exists
        existing_participant = db.group_participants.find_one({
            "group_id": group_id,
            "phone_number": participant.phone_number
        })
        if existing_participant:
            raise HTTPException(
                status_code=400,
                detail="Participant already exists in this group"
            )

        # Create participant document
        participant_doc = Participant(
            group_id=group_id,
            phone_number=participant.phone_number,
            name=participant.name,
            role=participant.role
        ).dict()

        # Insert into database
        result = db.group_participants.insert_one(participant_doc)
        
        # Get the created participant
        created_participant = db.group_participants.find_one({"_id": result.inserted_id})
        
        return Participant(**created_participant)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups/{group_id}/participants", response_model=List[Participant])
async def get_group_participants(
    group_id: str,
    current_user: UserInDB = Depends(get_current_active_user)
):
    """
    Get all participants for a specific group.
    """
    try:
        # Verify group exists
        group = db.groups.find_one({"group_sid": group_id})
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        # Get all active participants
        participants = list(db.group_participants.find({
            "group_id": group_id,
            "is_active": True
        }))
        
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
    
    Args:
        group_id: The WhatsApp group ID
        num_messages: Number of recent messages to consider for context
        
    Returns:
        Dict containing the generated reply and metadata
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
        
        # Create prompt for Groq
        prompt = f"""Based on the following WhatsApp conversation, generate a helpful and contextual response.
        The response should be professional, concise, and address any questions or concerns raised.
        
        Recent messages:
        {context}
        
        Generate a response:"""
        
        # Call Groq API
        completion = groq_client.chat.completions.create(
            model="mixtral-8x7b-32768",  # or another appropriate model
            messages=[
                {"role": "system", "content": "You are a helpful customer service assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=150
        )
        
        reply_text = completion.choices[0].message.content.strip()
        
        # Create auto-reply document
        auto_reply = {
            "group_id": group_id,
            "client_number": recent_messages[0]["sender"],  # Reply to the most recent message
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
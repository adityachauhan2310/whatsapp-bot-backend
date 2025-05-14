import os
import time
import logging
import requests
from typing import Dict, Optional, List
from pymongo import MongoClient
from dotenv import load_dotenv
import magic
from datetime import datetime
from ai_prompts import keyword_summary_prompt, text_summary_prompt
import certifi

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# MongoDB connection
client = MongoClient(os.getenv("MONGODB_URI"), tlsCAFile=certifi.where())
db = client[os.getenv("MONGODB_DB")]

def get_groq_client():
    import groq
    return groq.Client(api_key=os.getenv("GROQ_API_KEY"))

def process_text_with_groq(text: str) -> Dict:
    """Process text using Groq API to extract keywords and generate summary."""
    try:
        response = get_groq_client().chat.completions.create(
            messages=[
                {"role": "system", "content": keyword_summary_prompt},
                {"role": "user", "content": f"Extract key keywords and generate a summary for this text: {text}"}
            ],
            model="mixtral-8x7b-32768",
            temperature=0.3,
            max_tokens=150
        )
        result = response.choices[0].message.content
        parts = result.split("\n\n")
        keywords = [k.strip() for k in parts[0].split(":")[1].split(",")] if len(parts) > 0 and ":" in parts[0] else []
        summary = parts[1] if len(parts) > 1 else ""
        return {"keywords": keywords, "summary": summary}
    except Exception as e:
        logger.error(f"Error processing text with Groq: {str(e)}")
        return {"keywords": [], "summary": "", "error": str(e)}

def summarize_text(text: str) -> str:
    """Summarize text using Groq API."""
    try:
        response = get_groq_client().chat.completions.create(
            messages=[
                {"role": "system", "content": text_summary_prompt},
                {"role": "user", "content": f"Summarize this text: {text}"}
            ],
            model="mixtral-8x7b-32768",
            temperature=0.3,
            max_tokens=100
        )
        summary = response.choices[0].message.content.strip()
        return summary
    except Exception as e:
        logger.error(f"Error summarizing text with Groq: {str(e)}")
        return ""

def transcribe_voice(audio_url: Optional[str] = None) -> str:
    """Mock function to simulate voice transcription. Returns a sample transcribed text."""
    # In production, download and transcribe the audio file here.
    time.sleep(1.5)
    return "Meeting summary: The team discussed the new project timeline and resource allocation. Key points include the need for additional developers and a revised deadline of Q2 2024. Action items were assigned to team members."

def process_message(message_data: Dict) -> Dict:
    """
    Process incoming WhatsApp messages by type (text, voice, media, document).
    Uses Groq API for text analysis and summarization.
    Stores processed results in MongoDB.
    """
    try:
        message_type = message_data.get("type", "text")
        processed_data = {
            "message_id": message_data.get("id"),
            "from": message_data.get("from"),
            "type": message_type,
            "timestamp": time.time(),
            "status": "processing"
        }
        if message_type == "text":
            text = message_data.get("body", "")
            if not text:
                raise ValueError("Empty text message")
            result = process_text_with_groq(text)
            processed_data.update(result)
            processed_data["status"] = "completed"
        elif message_type == "voice":
            voice_url = message_data.get("media_url")
            if not voice_url:
                raise ValueError("No voice URL provided")
            transcribed_text = transcribe_voice(voice_url)
            result = process_text_with_groq(transcribed_text)
            processed_data.update({
                "transcription": transcribed_text,
                **result
            })
            processed_data["status"] = "completed"
        elif message_type in ["media", "document"]:
            media_url = message_data.get("media_url")
            if not media_url:
                raise ValueError("No media URL provided")
            # Use Twilio Auth for media download
            TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
            TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
            response = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
            if response.status_code == 200:
                file_type = magic.from_buffer(response.content, mime=True)
                processed_data.update({
                    "media_url": media_url,
                    "file_type": file_type,
                    "file_size": len(response.content),
                    "notes": f"Media type: {file_type}"
                })
            else:
                raise ValueError(f"Failed to download media: {response.status_code}")
            processed_data["status"] = "completed"
        else:
            raise ValueError(f"Unsupported message type: {message_type}")
        db.processed_messages.insert_one(processed_data)
        logger.info(f"Successfully processed message: {message_data.get('id')}")
        return processed_data
    except Exception as e:
        error_message = f"Error processing message: {str(e)}"
        logger.error(error_message)
        processed_data = {
            "message_id": message_data.get("id"),
            "from": message_data.get("from"),
            "type": message_data.get("type", "unknown"),
            "timestamp": time.time(),
            "status": "error",
            "error": error_message
        }
        db.processed_messages.insert_one(processed_data)
        return processed_data

def generate_daily_summary_for_group(group_sid: str) -> Dict:
    """Generate a daily summary for a group using Groq API."""
    try:
        yesterday = time.time() - 86400
        messages = list(db.processed_messages.find({
            "group_sid": group_sid,
            "timestamp": {"$gte": yesterday}
        }))
        if not messages:
            return {"group_sid": group_sid, "summary": "No messages in the last 24 hours."}
        text_to_summarize = "\n".join([
            f"{msg.get('from', 'Unknown')}: {msg.get('body', '')}" for msg in messages if msg.get('body')
        ])
        summary = summarize_text(text_to_summarize)
        summary_doc = {
            "group_sid": group_sid,
            "date": datetime.now().date(),
            "summary": summary,
            "message_count": len(messages)
        }
        db.daily_summaries.insert_one(summary_doc)
        logger.info(f"Generated daily summary for group {group_sid}")
        return summary_doc
    except Exception as e:
        logger.error(f"Error generating daily summary for group {group_sid}: {str(e)}")
        return {"group_sid": group_sid, "summary": "Error generating summary.", "error": str(e)}

def get_processed_messages(limit: int = 10) -> List[Dict]:
    """Retrieve processed messages from MongoDB."""
    try:
        return list(db.processed_messages.find().sort("timestamp", -1).limit(limit))
    except Exception as e:
        logger.error(f"Error retrieving processed messages: {str(e)}")
        return [] 
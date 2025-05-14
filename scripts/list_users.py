import os
import asyncio
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# Load environment variables
load_dotenv()

# MongoDB connection parameters
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB")

print(f"MONGODB_URI: {MONGODB_URI}")
print(f"MONGODB_DB: {MONGODB_DB}")

async def list_users():
    # Set up MongoDB connection
    client = AsyncIOMotorClient(
        MONGODB_URI,
        serverSelectionTimeoutMS=30000,
        connectTimeoutMS=45000,
        retryWrites=True,
    )
    db = client[MONGODB_DB]
    
    try:
        # List all users
        cursor = db.users.find({})
        users = await cursor.to_list(length=100)
        
        print(f"Found {len(users)} users:")
        for user in users:
            print(f"Username: {user.get('username')}")
            print(f"  Email: {user.get('email')}")
            print(f"  Role: {user.get('role')}")
            print(f"  Active: {user.get('is_active')}")
            print(f"  Verified: {user.get('is_verified')}")
            print(f"  Hash: {user.get('hashed_password')[:20]}...")
            print("---")
            
    except Exception as e:
        print(f"Error listing users: {str(e)}")
    finally:
        # Close connection
        client.close()
        print("Connection closed")

if __name__ == "__main__":
    # Run the async function
    asyncio.run(list_users()) 
import os
import asyncio
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import certifi

# Load environment variables
load_dotenv()

# MongoDB connection parameters
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB")

async def test_connection():
    # Set up MongoDB connection
    client = AsyncIOMotorClient(
        MONGODB_URI,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=30000,
        socketTimeoutMS=45000,
        connectTimeoutMS=45000,
        retryWrites=True,
        tlsAllowInvalidCertificates=False
    )
    db = client[MONGODB_DB]
    
    try:
        # Simple ping to check connection
        await db.command("ping")
        print("MongoDB connection successful!")
        
        # Check if we can retrieve data
        users_count = await db.users.count_documents({})
        print(f"Users count: {users_count}")
        
        # Get one document
        admin = await db.users.find_one({"username": "admin"})
        if admin:
            print(f"Found admin user: {admin.get('email')}")
        else:
            print("Admin user not found")
            
    except Exception as e:
        print(f"Error testing MongoDB connection: {str(e)}")
    finally:
        # Close connection
        client.close()
        print("Connection closed")

if __name__ == "__main__":
    # Run the async function
    asyncio.run(test_connection()) 
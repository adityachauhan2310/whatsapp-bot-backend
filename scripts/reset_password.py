import os
import asyncio
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext

# Load environment variables
load_dotenv()

# MongoDB connection parameters
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB")

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

print(f"MONGODB_URI: {MONGODB_URI}")
print(f"MONGODB_DB: {MONGODB_DB}")

async def reset_password(username, new_password):
    # Set up MongoDB connection
    client = AsyncIOMotorClient(
        MONGODB_URI,
        serverSelectionTimeoutMS=30000,
        connectTimeoutMS=45000,
        retryWrites=True,
    )
    db = client[MONGODB_DB]
    
    try:
        # Find user
        user = await db.users.find_one({"username": username})
        if not user:
            print(f"User {username} not found!")
            return False
        
        # Hash the new password
        hashed_password = pwd_context.hash(new_password)
        
        # Update user
        result = await db.users.update_one(
            {"username": username},
            {"$set": {"hashed_password": hashed_password}}
        )
        
        if result.modified_count > 0:
            print(f"Successfully reset password for {username}")
            return True
        else:
            print(f"Failed to update password for {username}")
            return False
            
    except Exception as e:
        print(f"Error resetting password: {str(e)}")
        return False
    finally:
        # Close connection
        client.close()
        print("Connection closed")

if __name__ == "__main__":
    # Username and new password
    username = "aditya2310chauhan"
    new_password = "Admin123!"
    
    # Run the async function
    asyncio.run(reset_password(username, new_password))
    
    print(f"Password reset script complete for {username}")
    print(f"New password: {new_password}") 
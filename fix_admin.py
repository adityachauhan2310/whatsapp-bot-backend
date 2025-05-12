from pymongo import MongoClient
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Connect to MongoDB
client = MongoClient(os.getenv("MONGODB_URI"))
db = client[os.getenv("MONGODB_DB")]

print("Starting admin role fix...")

# Find admin users
admin_users = db.users.find({"role": {"$regex": "admin", "$options": "i"}})
for user in admin_users:
    db.users.update_one(
        {"_id": user["_id"]}, 
        {"$set": {"role": "admin"}}
    )
    print(f"Updated user {user.get('username')} role to 'admin'")

# Show all users
print("\nUsers in database:")
for user in db.users.find():
    print(f"- {user.get('username')}: {user.get('role')}")

print("\nScript complete!") 
"""
Fix admin role casing in the database
"""
from pymongo import MongoClient
import os
from dotenv import load_dotenv
from passlib.context import CryptContext

# Load environment variables
print("Loading environment variables...")
load_dotenv()

# Connect to MongoDB
print("Connecting to MongoDB...")
client = MongoClient(os.getenv("MONGODB_URI"))
db = client[os.getenv("MONGODB_DB")]

# Password context for creating users
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Print all users
print("\nExisting users:")
users = list(db.users.find())
for user in users:
    print(f"- {user.get('username')}: Role={user.get('role')}")

# Fix admin roles
print("\nFixing admin roles...")
for user in users:
    username = user.get("username")
    role = user.get("role")
    
    if role and role.lower() == 'admin' and role != 'admin':
        print(f"Updating {username} role from '{role}' to 'admin'")
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"role": "admin"}}
        )

# Create admin user if needed
admin_user = db.users.find_one({"username": "adminuser"})
if not admin_user:
    print("\nCreating admin user...")
    user_doc = {
        "username": "adminuser",
        "email": "admin@example.com",
        "hashed_password": pwd_context.hash("adminpassword"),
        "full_name": "Admin User",
        "role": "admin",
        "is_active": True,
        "is_verified": True,
        "verification_token": None,
        "refresh_tokens": []
    }
    db.users.insert_one(user_doc)
    print("Created admin user: adminuser / adminpassword")
else:
    print("\nAdmin user already exists")
    # Ensure admin user has correct role
    if admin_user.get("role") != "admin":
        db.users.update_one(
            {"username": "adminuser"},
            {"$set": {"role": "admin"}}
        )
        print("Updated admin user role to 'admin'")

# Verify the changes
print("\nUsers after fixes:")
updated_users = list(db.users.find())
for user in updated_users:
    print(f"- {user.get('username')}: Role={user.get('role')}")

print("\nFix complete!") 
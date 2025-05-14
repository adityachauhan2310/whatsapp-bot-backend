#!/usr/bin/env python3
"""
This script checks for and corrects admin user role casing in the database.
It ensures that all admin roles are consistently lower case ('admin') to match
the backend's expectations.
"""

from pymongo import MongoClient
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Connect to MongoDB
def get_db():
    try:
        client = MongoClient(os.getenv("MONGODB_URI"))
        db = client[os.getenv("MONGODB_DB")]
        return db
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return None

def fix_admin_roles():
    db = get_db()
    if db is None:
        print("Failed to connect to database")
        return
    
    # Find all users that might have admin role with any casing
    potential_admins = list(db.users.find({
        "$or": [
            {"role": {"$regex": "admin", "$options": "i"}},
            {"role": {"$regex": "ADMIN", "$options": "i"}}
        ]
    }))
    
    print(f"Found {len(potential_admins)} potential admin users")
    
    # Log and fix each user
    for user in potential_admins:
        username = user.get("username", "Unknown")
        original_role = user.get("role", "Unknown")
        
        print(f"User: {username}, Current role: {original_role}")
        
        # Standardize to lowercase 'admin'
        if original_role.lower() == 'admin' and original_role != 'admin':
            db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"role": "admin"}}
            )
            print(f"  ✓ Updated role from '{original_role}' to 'admin'")
        elif original_role.lower() != 'admin':
            print(f"  ✗ Not an admin role or already lowercase")
        else:
            print(f"  ✓ Already using correct lowercase 'admin'")
    
    # Print current admin users for verification
    print("\nCurrent admin users after fixes:")
    admins = list(db.users.find({"role": "admin"}))
    for admin in admins:
        print(f"- {admin.get('username')}: {admin.get('role')}")

def create_test_admin():
    """Create a test admin user if none exists"""
    db = get_db()
    if db is None:
        return
    
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    
    # Check if the admin user already exists
    admin = db.users.find_one({"username": "adminuser"})
    if admin:
        print(f"Admin user already exists: {admin.get('username')}, role: {admin.get('role')}")
        return
    
    # Create a new admin user
    user_doc = {
        "username": "adminuser",
        "email": "admin@example.com",
        "hashed_password": pwd_context.hash("adminpassword"),
        "full_name": "Admin User",
        "role": "admin",  # Note: using lowercase consistently
        "is_active": True,
        "is_verified": True,
        "verification_token": None,
        "refresh_tokens": []
    }
    
    db.users.insert_one(user_doc)
    print("Created new admin user: adminuser / adminpassword")

if __name__ == "__main__":
    print("Starting admin role verification and correction...")
    fix_admin_roles()
    create_test_admin()
    print("Completed admin role verification and correction.") 
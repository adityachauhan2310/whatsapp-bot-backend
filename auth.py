from datetime import datetime, timedelta
from typing import Optional, List
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from models import UserRole
import os
import secrets
import string
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# MongoDB connection
client = MongoClient(os.getenv("MONGODB_URI"))
db = client[os.getenv("MONGODB_DB")]

# JWT Configuration
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-here")  # Change this in production
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

class Token(BaseModel):
    access_token: str
    token_type: str
    refresh_token: Optional[str] = None
    username: str

class TokenData(BaseModel):
    username: Optional[str] = None
    role: Optional[UserRole] = None

class User(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None
    role: UserRole = UserRole.USER
    is_active: bool = True
    is_verified: bool = False

class UserInDB(User):
    hashed_password: str
    verification_token: Optional[str] = None
    reset_token: Optional[str] = None
    refresh_tokens: Optional[List[str]] = []

class UserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    role: Optional[UserRole] = None

# Utility functions

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def create_refresh_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode = {"sub": username, "exp": expire, "type": "refresh"}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def generate_token(length=32):
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))

# Email verification
async def send_verification_email(email: str, token: str):
    # Compose the confirmation link
    confirmation_link = f"http://localhost:8000/api/confirm-email?token={token}"
    subject = "Confirm your email for WhatsApp Bot Dashboard"
    body = f"Click the link below to confirm your email and activate your account: {confirmation_link}"
    # TODO: Integrate with an email service
    print(f"To: {email}\nSubject: {subject}\nBody: {body}")

async def verify_email(token: str):
    user = db.users.find_one({"verification_token": token})
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")
    db.users.update_one({"_id": user["_id"]}, {"$set": {"is_verified": True, "verification_token": None}})
    return {"message": "Email verified successfully."}

# Password reset
async def send_reset_email(email: str, token: str):
    # TODO: Integrate with an email service
    print(f"Send password reset email to {email} with token: {token}")

async def reset_password(token: str, new_password: str):
    user = db.users.find_one({"reset_token": token})
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    hashed = get_password_hash(new_password)
    db.users.update_one({"_id": user["_id"]}, {"$set": {"hashed_password": hashed, "reset_token": None}})
    return {"message": "Password reset successful."}

# Session management
async def add_refresh_token(username: str, refresh_token: str):
    db.users.update_one({"username": username}, {"$push": {"refresh_tokens": refresh_token}})

async def remove_refresh_token(username: str, refresh_token: str):
    db.users.update_one({"username": username}, {"$pull": {"refresh_tokens": refresh_token}})

# Role-based permission checks
async def get_current_user(token: str = Depends(oauth2_scheme)) -> UserInDB:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        role: str = payload.get("role")
        if username is None or role is None:
            raise credentials_exception
        token_data = TokenData(username=username, role=role)
    except JWTError:
        raise credentials_exception
    user = db.users.find_one({"username": token_data.username})
    if not user:
        raise credentials_exception
    return UserInDB(**user)

async def get_current_active_user(current_user: UserInDB = Depends(get_current_user)) -> UserInDB:
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    if not current_user.is_verified:
        raise HTTPException(status_code=400, detail="Email not verified")
    return current_user

async def get_current_admin_user(current_user: UserInDB = Depends(get_current_active_user)) -> UserInDB:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user doesn't have enough privileges"
        )
    return current_user

# Middleware for role-based protection
async def require_role(request: Request, required_role: UserRole):
    user: UserInDB = await get_current_user(request.headers.get("Authorization").split()[1])
    if user.role != required_role:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return user 
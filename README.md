# WhatsApp Bot Backend

A FastAPI backend for processing and managing WhatsApp messages.

## Setup Instructions

1. **Clone the repository**
   ```bash
   git clone https://github.com/adityachauhan2310/whatsapp-bot-backend.git
   cd whatsapp-bot-backend
   ```

2. **Create and activate a virtual environment**
   ```bash
   python -m venv venv
   # On Windows
   .\venv\Scripts\activate
   # On macOS/Linux
   source venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Setup environment variables**
   Create a `.env` file in the root directory with the following variables:
   ```
   TWILIO_ACCOUNT_SID=your_twilio_account_sid
   TWILIO_AUTH_TOKEN=your_twilio_auth_token
   TWILIO_PHONE_NUMBER=your_twilio_phone_number
   TWILIO_WHATSAPP_FROM=your_twilio_whatsapp_number
   MONGODB_URI=your_mongodb_connection_string
   MONGODB_DB=your_mongodb_database_name
   JWT_SECRET_KEY=your_jwt_secret_key
   ```

5. **Run the server**
   ```bash
   # Using the script (Windows)
   .\start_server.bat
   
   # Using uvicorn directly
   uvicorn main:app --reload --host 127.0.0.1 --port 8000
   ```

6. **Access the API documentation**
   Open your browser and navigate to:
   ```
   http://127.0.0.1:8000/docs
   ```

## Project Structure

- **main.py**: Main FastAPI application and endpoints
- **auth.py**: Authentication, user management, and login functions
- **models.py**: Pydantic data models
- **utils.py**: Utility functions
- **scheduler.py**: Background task scheduling
- **ai_prompts.py**: AI prompt templates
- **scripts/**: Utility scripts for maintenance and testing
  - **fix_admin.py**: Script to fix admin user roles
  - **test_db.py**: Test MongoDB connection
  - **verify_admin.py**: Verify admin roles
  - **fix_roles.py**: Fix user roles
  - **rehash_password.py**: Reset user passwords

## Default Admin Credentials

If you're setting up a new system, a default admin user will be created:
- Username: aditya2310chauhan
- Password: Admin123!

For security reasons, it's recommended to change this password after the first login.

## Troubleshooting

- **Login Issues**: If you encounter login issues, you can reset the admin password using the `scripts/reset_password.py` script.
- **MongoDB Connection**: Verify your MongoDB connection string in the `.env` file and run `scripts/test_db.py` to test the connection.
- **Webhook Errors**: Make sure your Twilio webhook URL is set correctly and points to your server's `/webhook/twilio` endpoint.

## API Endpoints

- **Authentication**:
  - POST `/api/login`: JSON login endpoint
  - POST `/api/token`: OAuth2 token endpoint
  - POST `/api/register`: User registration

- **Groups**:
  - GET `/api/groups`: List all groups
  - POST `/api/groups`: Create a new group
  - GET `/api/groups/{group_sid}`: Get a specific group
  - PUT `/api/groups/{group_sid}`: Update a group
  - DELETE `/api/groups/{group_sid}`: Delete a group

- **Messages**:
  - GET `/api/messages/processed`: Get processed messages
  - GET `/api/messages/search`: Search messages

- **System**:
  - GET `/api/system/status`: Get system status (admin only)
  - GET `/api/system/audit-logs`: Get audit logs (admin only)

- **Analytics**:
  - GET `/api/analytics/group/{group_id}`: Get group analytics
  - GET `/api/analytics/daily/{group_id}`: Get daily statistics
  - GET `/api/analytics/messages`: Get message analytics 
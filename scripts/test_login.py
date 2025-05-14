import requests
import json

def test_login():
    url = "http://127.0.0.1:8001/api/login"
    
    # Admin credentials - updated
    payload = {
        "username": "aditya2310chauhan",
        "password": "Admin123!",  # Updated password
        "remember_me": False
    }
    
    headers = {
        "Content-Type": "application/json"
    }
    
    print(f"Sending login request to {url} with payload: {payload}")
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        print(f"Status code: {response.status_code}")
        
        if response.status_code == 200:
            # Try to parse response as JSON
            try:
                data = response.json()
                print("Login successful!")
                print(f"Access token: {data.get('access_token')[:20]}...")
                print(f"Username: {data.get('username')}")
                return True
            except json.JSONDecodeError as e:
                print(f"Error parsing JSON response: {str(e)}")
                print(f"Response text: {response.text}")
                return False
        else:
            print(f"Login failed with status {response.status_code}")
            print(f"Response: {response.text}")
            return False
    except Exception as e:
        print(f"Exception during login request: {str(e)}")
        return False

if __name__ == "__main__":
    test_login() 
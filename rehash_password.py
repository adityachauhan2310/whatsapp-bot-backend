from auth import get_password_hash
 
password = input('Enter the new password to hash: ')
hashed = get_password_hash(password)
print('Hashed password:', hashed) 
import getpass
import sys
from app import database, ph
username = sys.argv[1]
password = getpass.getpass('New password: ')
if len(password) < 12:
    raise SystemExit('Use at least 12 characters')
if password != getpass.getpass('Repeat password: '):
    raise SystemExit('Passwords differ')
with database() as db:
    db.execute('INSERT INTO users(username,password_hash) VALUES (%s,%s)',(username,ph.hash(password)))

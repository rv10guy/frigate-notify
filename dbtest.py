import sqlite3
import time

silence_db = 'silence_settings.db'  # Replace with the path to your database file

def dump_database():
    while True:
        conn = sqlite3.connect(silence_db)
        c = conn.cursor()
        c.execute('SELECT * FROM silence_settings')
        rows = c.fetchall()
        conn.close()
        print("\033c", end="")  # Clear the console
        print(f"Database Dump (refreshing every 5 seconds):")
        for row in rows:
            print(row)
        time.sleep(5)

if __name__ == "__main__":
    dump_database()

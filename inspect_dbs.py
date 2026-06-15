import sqlite3
import os

databases = ['crawler.db', 'library.db', 'watchlist.db']
base_path = r'c:\Users\NLSur\OneDrive\Documents\MediaScout'

for db_name in databases:
    db_path = os.path.join(base_path, db_name)
    if not os.path.exists(db_path):
        print(f"Database {db_name} not found at {db_path}")
        continue
    
    print(f"\n{'='*50}")
    print(f"DATABASE: {db_name}")
    print(f"{'='*50}")
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get list of tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0] for t in cursor.fetchall() if not t[0].startswith('sqlite_')]
        
        for table in tables:
            print(f"\nTABLE: {table}")
            # Get table schema
            cursor.execute(f"PRAGMA table_info({table});")
            columns = cursor.fetchall()
            for col in columns:
                # col format: (id, name, type, notnull, dflt_value, pk)
                print(f"  - {col[1]} ({col[2]})" + (" [PK]" if col[5] else ""))
                
        conn.close()
    except Exception as e:
        print(f"Error inspecting {db_name}: {e}")

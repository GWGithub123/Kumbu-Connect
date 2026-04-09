"""
Add cbo_identifier column to CBOs table.
"""
import sys
import os
sys.path.insert(0, os.path.abspath('.'))

from webapp import create_app
from webapp.models import db
from sqlalchemy import text

def migrate():
    app = create_app()
    
    with app.app_context():
        print("Adding cbo_identifier column to cbos table...")
        
        try:
            # Add the column
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE cbos ADD COLUMN cbo_identifier VARCHAR(50)"))
                conn.commit()
            
            print("✓ Column added successfully!")
            print("\nRun seed.py to populate the identifiers.")
            
        except Exception as e:
            if "duplicate column" in str(e).lower():
                print("✓ Column already exists")
            else:
                print(f"Error: {e}")

if __name__ == '__main__':
    migrate()

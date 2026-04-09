"""
Database migration script to add growth_metrics_json column to existing CBOs.
"""
import sys
import os
sys.path.insert(0, os.path.abspath('.'))

from webapp import create_app, db
from webapp.models import CBO

def migrate():
    app = create_app()
    
    with app.app_context():
        print("Running database migration...")
        
        # Check if column exists by trying to query it
        try:
            test = CBO.query.with_entities(CBO.growth_metrics_json).first()
            print("✓ growth_metrics_json column already exists")
        except Exception as e:
            # Column doesn't exist, need to add it
            print(f"Adding growth_metrics_json column...")
            from sqlalchemy import text
            
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE cbos ADD COLUMN growth_metrics_json TEXT DEFAULT '[]'"))
                conn.commit()
            
            print("✓ Migration complete!")
        
        # Initialize all CBOs with empty growth data
        cbos = CBO.query.all()
        for cbo in cbos:
            if not cbo.growth_metrics_json or cbo.growth_metrics_json == '':
                cbo.growth_metrics_json = '[]'
        db.session.commit()
        
        print(f"✓ Initialized {len(cbos)} CBOs with empty growth metrics")
        print("\nDone! Run sync_all.py to populate growth metrics.")

if __name__ == '__main__':
    migrate()

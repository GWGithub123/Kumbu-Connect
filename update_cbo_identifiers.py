"""
Update existing CBOs with their cbo_identifier values.
"""
import sys
import os
sys.path.insert(0, os.path.abspath('.'))

from webapp import create_app
from webapp.models import db, CBO

def update_identifiers():
    app = create_app()
    
    with app.app_context():
        print("Updating CBO identifiers...")
        
        # Map CBO names to their identifiers
        identifier_map = {
            "Busia Community Tool Hub": "tools",
            "Kakamega Farm Collective": "tools",
            "Bright Futures Education CBO": "education",
            "Health For All CBO": "healthcare",
            "Green Harvest Agriculture CBO": "agriculture",
            "Clean Water Initiative CBO": "water"
        }
        
        for cbo in CBO.query.all():
            if cbo.name in identifier_map:
                cbo.cbo_identifier = identifier_map[cbo.name]
                print(f"  ✓ {cbo.name} -> {cbo.cbo_identifier}")
        
        db.session.commit()
        print("\n✅ All CBOs updated with identifiers!")

if __name__ == '__main__':
    update_identifiers()

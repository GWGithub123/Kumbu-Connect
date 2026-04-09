"""
Tool Rental Trial Program - Create Form and Upload Data to Kobo
"""

import requests
import json
from datetime import datetime, timedelta
import random
import time

# Kobo Toolbox Configuration
KOBO_API_KEY = "7327ad2b882fb5d4975916811759dc339b266cd4"
KOBO_BASE_URL = "https://kf.kobotoolbox.org/api/v2"

# Sample data for generation
TOOLS = [
    "Hammer", "Drill", "Saw", "Screwdriver Set", "Wrench Set",
    "Pliers", "Level", "Tape Measure", "Ladder", "Power Drill",
    "Circular Saw", "Sander", "Paint Roller", "Garden Hose", "Wheelbarrow"
]

BORROWERS = [
    "Jesse Artache", "Isaac Simkin", "Israel Simmons", "Lydia Anwor",
    "Danek Obriondo", "Derek Obriondo", "Clement Jackson", "Cassanova Miller",
    "Maria Rodriguez", "John Thompson", "Sarah Williams", "Michael Chen", "Patricia Davis"
]

CONDITIONS = [
    "Good condition upon return", "Good Condition upon Return",
    "Excellent condition", "Minor wear, acceptable", "Needs cleaning", "Small scratch noted"
]

DAMAGE_CHARGES = ["—", "—", "—", "—", "$5 cleaning fee", "$10 minor damage", "$15 repair needed", "No charge"]

def create_tool_rental_form():
    """Create a new Kobo form for tool rentals"""
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Define the form structure
    form_content = {
        "survey": [
            {"type": "date", "name": "date_loaned", "label": "Date Loaned", "required": True},
            {"type": "time", "name": "time_loaned", "label": "Time Loaned"},
            {"type": "date", "name": "date_returned", "label": "Date Returned"},
            {"type": "time", "name": "time_returned", "label": "Time Returned"},
            {"type": "text", "name": "tool_name", "label": "Tool Name", "required": True},
            {"type": "text", "name": "borrower_name", "label": "Borrower Name", "required": True},
            {"type": "text", "name": "borrower_signature", "label": "Borrower Signature"},
            {"type": "text", "name": "condition_upon_return", "label": "Condition Upon Return"},
            {"type": "text", "name": "damage_charged", "label": "Damage Charged"},
            {"type": "text", "name": "return_notes", "label": "Return Notes"},
            {"type": "text", "name": "serial_number", "label": "Serial Number"},
            {"type": "integer", "name": "quantity", "label": "Quantity"}
        ],
        "settings": {
            "form_title": "Tool Rental Trial Program",
            "form_id": "tool_rental_trial"
        }
    }
    
    payload = {
        "name": "Tool Rental Trial Program",
        "asset_type": "survey",
        "content": json.dumps(form_content),
        "settings": {
            "description": "Form for tracking tool rentals in trial program",
            "sector": {"label": "Humanitarian - Logistics", "value": "Humanitarian - Logistics"},
            "country": [{"label": "United States", "value": "USA"}]
        }
    }
    
    try:
        print("Creating new form in Kobo Toolbox...")
        response = requests.post(f"{KOBO_BASE_URL}/assets/", headers=headers, json=payload)
        
        if response.status_code in [200, 201]:
            asset = response.json()
            asset_id = asset['uid']
            print(f"✓ Form created successfully! Asset ID: {asset_id}")
            
            # Deploy the form
            print("Deploying form...")
            deploy_response = requests.post(
                f"{KOBO_BASE_URL}/assets/{asset_id}/deployment/",
                headers=headers,
                json={"active": True}
            )
            
            if deploy_response.status_code in [200, 201]:
                print("✓ Form deployed successfully!")
                return asset_id
            else:
                print(f"⚠ Form created but deployment failed: {deploy_response.status_code}")
                print(f"Response: {deploy_response.text}")
                return asset_id
        else:
            print(f"✗ Failed to create form: {response.status_code}")
            print(f"Response: {response.text}")
            return None
            
    except Exception as e:
        print(f"✗ Error creating form: {str(e)}")
        return None

def generate_sample_data(num_records=30):
    """Generate sample tool rental records"""
    records = []
    start_date = datetime.now() - timedelta(days=90)
    
    for i in range(num_records):
        loan_date = start_date + timedelta(days=random.randint(0, 90))
        loan_time = f"{random.randint(8, 17):02d}:{random.choice(['00', '15', '30', '45'])}"
        
        days_borrowed = random.randint(1, 14)
        return_date = loan_date + timedelta(days=days_borrowed)
        return_time = f"{random.randint(8, 17):02d}:{random.choice(['00', '15', '30', '45'])}"
        
        record = {
            "date_loaned": loan_date.strftime("%Y-%m-%d"),
            "time_loaned": loan_time,
            "date_returned": return_date.strftime("%Y-%m-%d"),
            "time_returned": return_time,
            "tool_name": random.choice(TOOLS),
            "borrower_name": random.choice(BORROWERS),
            "borrower_signature": random.choice(BORROWERS).split()[0],
            "condition_upon_return": random.choice(CONDITIONS),
            "damage_charged": random.choice(DAMAGE_CHARGES),
            "return_notes": random.choice([
                "On time return", "Late return, no charge", "Returned clean",
                "Needs inspection", "Good borrower", ""
            ]),
            "serial_number": f"TOOL-{1000 + i}",
            "quantity": random.randint(1, 4)
        }
        
        records.append(record)
    
    return records

def upload_to_kobo(records, asset_id):
    """Upload data to Kobo Toolbox via API using CSV bulk import"""
    import csv
    import io
    
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
    }
    
    # Create CSV in memory
    csv_buffer = io.StringIO()
    if records:
        writer = csv.DictWriter(csv_buffer, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
    
    csv_content = csv_buffer.getvalue()
    csv_buffer.close()
    
    # Upload CSV
    url = f"{KOBO_BASE_URL}/assets/{asset_id}/data.json"
    
    files = {
        'file': ('data.csv', csv_content, 'text/csv')
    }
    
    print(f"\nUploading {len(records)} records via CSV bulk import...")
    
    try:
        response = requests.post(url, headers=headers, files=files)
        
        if response.status_code in [200, 201, 202]:
            print(f"✓ All records uploaded successfully!")
            return {
                "successful": len(records),
                "failed": 0,
                "errors": []
            }
        else:
            print(f"✗ Upload failed: {response.status_code}")
            print(f"Response: {response.text}")
            return {
                "successful": 0,
                "failed": len(records),
                "errors": [f"{response.status_code} - {response.text}"]
            }
            
    except Exception as e:
        print(f"✗ Error uploading: {str(e)}")
        return {
            "successful": 0,
            "failed": len(records),
            "errors": [str(e)]
        }

def main():
    print("=" * 70)
    print("Tool Rental Trial Program - Kobo Toolbox Setup & Upload")
    print("=" * 70)
    
    # Step 1: Create the form
    print("\n1. Creating form in Kobo Toolbox...")
    asset_id = create_tool_rental_form()
    
    if not asset_id:
        print("\n✗ Failed to create form. Exiting.")
        return
    
    print(f"\nForm URL: https://kf.kobotoolbox.org/#/forms/{asset_id}/summary")
    
    # Wait a moment for form to be ready
    print("\nWaiting for form to be fully deployed...")
    time.sleep(3)
    
    # Step 2: Generate sample data
    print("\n2. Generating sample data...")
    num_records = 30
    records = generate_sample_data(num_records)
    print(f"✓ Generated {len(records)} sample records")
    
    # Step 3: Upload to Kobo
    print("\n3. Uploading data to Kobo Toolbox...")
    print("-" * 70)
    results = upload_to_kobo(records, asset_id)
    
    # Step 4: Display results
    print("\n" + "=" * 70)
    print("Upload Results:")
    print(f"  ✓ Successful uploads: {results['successful']}")
    print(f"  ✗ Failed uploads: {results['failed']}")
    
    if results['errors']:
        print("\nErrors encountered:")
        for error in results['errors'][:10]:
            print(f"  - {error}")
        if len(results['errors']) > 10:
            print(f"  ... and {len(results['errors']) - 10} more errors")
    
    print("\n" + "=" * 70)
    print("Process Complete!")
    print(f"View your data at: https://kf.kobotoolbox.org/#/forms/{asset_id}/data/table")
    print("=" * 70)

if __name__ == "__main__":
    main()

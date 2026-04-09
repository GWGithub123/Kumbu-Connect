"""
Upload Tool Rental Data to Existing Kobo Form
"""

import requests
import json
from datetime import datetime, timedelta
import random
import time

# Kobo Toolbox Configuration
KOBO_API_KEY = "7327ad2b882fb5d4975916811759dc339b266cd4"
KOBO_ASSET_ID = "aJ7GEDZPU3dbM4KAEqUBKW"  # The form we just created
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
            "quantity": str(random.randint(1, 4))  # Convert to string for submission
        }
        
        records.append(record)
    
    return records

def upload_submissions_individually(records, asset_id):
    """Upload submissions one by one using submissions endpoint"""
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Get the deployment to find submission URL
    deployment_url = f"{KOBO_BASE_URL}/assets/{asset_id}/deployment/"
    dep_response = requests.get(deployment_url, headers=headers)
    
    if dep_response.status_code != 200:
        print(f"✗ Failed to get deployment info: {dep_response.status_code}")
        return {"successful": 0, "failed": len(records), "errors": [dep_response.text]}
    
    deployment = dep_response.json()
    
    results = {
        "successful": 0,
        "failed": 0,
        "errors": []
    }
    
    print(f"\nUploading {len(records)} records individually...")
    
    for i, record in enumerate(records, 1):
        try:
            # Use submissions endpoint
            url = f"{KOBO_BASE_URL}/submissions/"
            
            # Format submission
            payload = {
                "id": f"tool_rental_{int(time.time())}_{i}",
                "formhub/uuid": asset_id,
                **record
            }
            
            response = requests.post(url, headers=headers, json=payload)
            
            if response.status_code in [200, 201, 202]:
                results["successful"] += 1
                print(f"✓ Record {i}/{len(records)} uploaded")
            else:
                # Try alternative endpoint
                alt_url = f"{KOBO_BASE_URL}/assets/{asset_id}/data/"
                alt_response = requests.post(alt_url, headers=headers, json=record)
                
                if alt_response.status_code in [200, 201, 202]:
                    results["successful"] += 1
                    print(f"✓ Record {i}/{len(records)} uploaded (alt method)")
                else:
                    results["failed"] += 1
                    error_msg = f"Record {i}: {response.status_code} - {response.text[:100]}"
                    results["errors"].append(error_msg)
                    print(f"✗ {error_msg}")
            
            time.sleep(0.2)  # Rate limiting
                
        except Exception as e:
            results["failed"] += 1
            error_msg = f"Record {i}: {str(e)}"
            results["errors"].append(error_msg)
            print(f"✗ {error_msg}")
    
    return results

def upload_via_csv(records, asset_id):
    """Upload data via CSV import"""
    import csv
    import tempfile
    import os
    
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
    }
    
    # Create temporary CSV file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, newline='') as f:
        csv_path = f.name
        if records:
            writer = csv.DictWriter(f, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)
    
    print(f"\nUploading {len(records)} records via CSV import...")
    print(f"CSV file created at: {csv_path}")
    
    try:
        # Try import endpoint
        url = f"{KOBO_BASE_URL}/assets/{asset_id}/data/"
        
        with open(csv_path, 'rb') as csv_file:
            files = {'file': ('data.csv', csv_file, 'text/csv')}
            response = requests.post(url, headers=headers, files=files)
        
        print(f"Response status: {response.status_code}")
        print(f"Response: {response.text[:500]}")
        
        os.unlink(csv_path)
        
        if response.status_code in [200, 201, 202]:
            return {
                "successful": len(records),
                "failed": 0,
                "errors": []
            }
        else:
            return {
                "successful": 0,
                "failed": len(records),
                "errors": [f"{response.status_code} - {response.text}"]
            }
            
    except Exception as e:
        if os.path.exists(csv_path):
            os.unlink(csv_path)
        return {
            "successful": 0,
            "failed": len(records),
            "errors": [str(e)]
        }

def main():
    print("=" * 70)
    print("Uploading Data to Kobo Toolbox Form")
    print(f"Form ID: {KOBO_ASSET_ID}")
    print("=" * 70)
    
    # Generate sample data
    print("\n1. Generating sample data...")
    records = generate_sample_data(30)
    print(f"✓ Generated {len(records)} sample records")
    
    # Show sample
    print("\nSample record:")
    print(json.dumps(records[0], indent=2))
    
    # Try CSV upload first
    print("\n2. Attempting CSV bulk upload...")
    print("-" * 70)
    results = upload_via_csv(records, KOBO_ASSET_ID)
    
    # If CSV fails, try individual submissions
    if results["failed"] > 0:
        print("\nCSV upload failed. Trying individual submissions...")
        print("-" * 70)
        results = upload_submissions_individually(records, KOBO_ASSET_ID)
    
    # Display results
    print("\n" + "=" * 70)
    print("Upload Results:")
    print(f"  ✓ Successful: {results['successful']}")
    print(f"  ✗ Failed: {results['failed']}")
    
    if results['errors'] and results['successful'] == 0:
        print("\nErrors:")
        for error in results['errors'][:5]:
            print(f"  - {error}")
    
    print("\n" + "=" * 70)
    print(f"View data: https://kf.kobotoolbox.org/#/forms/{KOBO_ASSET_ID}/data/table")
    print("=" * 70)

if __name__ == "__main__":
    main()

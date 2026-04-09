"""
Tool Rental Trial Program - Kobo Toolbox Data Upload Script
Generates sample data and uploads to Kobo Toolbox API
"""

import requests
import json
from datetime import datetime, timedelta
import random

# Kobo Toolbox Configuration
KOBO_API_KEY = "7327ad2b882fb5d4975916811759dc339b266cd4"
KOBO_BASE_URL = "https://kf.kobotoolbox.org/api/v2"
KOBO_ASSET_ID = "YOUR_FORM_ID_HERE"  # Replace with your actual form ID

# Sample data for generation
TOOLS = [
    "Hammer",
    "Drill",
    "Saw",
    "Screwdriver Set",
    "Wrench Set",
    "Pliers",
    "Level",
    "Tape Measure",
    "Ladder",
    "Power Drill",
    "Circular Saw",
    "Sander",
    "Paint Roller",
    "Garden Hose",
    "Wheelbarrow"
]

BORROWERS = [
    "Jesse Artache",
    "Isaac Simkin",
    "Israel Simmons",
    "Lydia Anwor",
    "Danek Obriondo",
    "Derek Obriondo",
    "Clement Jackson",
    "Cassanova Miller",
    "Maria Rodriguez",
    "John Thompson",
    "Sarah Williams",
    "Michael Chen",
    "Patricia Davis"
]

CONDITIONS = [
    "Good condition upon return",
    "Good Condition upon Return",
    "Excellent condition",
    "Minor wear, acceptable",
    "Needs cleaning",
    "Small scratch noted"
]

DAMAGE_CHARGES = [
    "—",
    "—",
    "—",
    "—",
    "$5 cleaning fee",
    "$10 minor damage",
    "$15 repair needed",
    "No charge"
]

def generate_sample_data(num_records=30):
    """Generate sample tool rental records"""
    records = []
    start_date = datetime.now() - timedelta(days=90)
    
    for i in range(num_records):
        # Generate loan date
        loan_date = start_date + timedelta(days=random.randint(0, 90))
        loan_time = f"{random.randint(8, 17):02d}:{random.choice(['00', '15', '30', '45'])}"
        
        # Generate return date (1-14 days later)
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
            "borrower_signature": random.choice(BORROWERS).split()[0],  # First name as signature
            "condition_upon_return": random.choice(CONDITIONS),
            "damage_charged": random.choice(DAMAGE_CHARGES),
            "return_notes": random.choice([
                "On time return",
                "Late return, no charge",
                "Returned clean",
                "Needs inspection",
                "Good borrower",
                ""
            ]),
            "serial_number": f"TOOL-{1000 + i}",
            "quantity": random.randint(1, 4)
        }
        
        records.append(record)
    
    return records

def upload_to_kobo(records, asset_id):
    """Upload data to Kobo Toolbox via API"""
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    url = f"{KOBO_BASE_URL}/assets/{asset_id}/data/"
    
    results = {
        "successful": 0,
        "failed": 0,
        "errors": []
    }
    
    for i, record in enumerate(records, 1):
        try:
            # Format data for Kobo submission
            submission = {
                "submission": record
            }
            
            response = requests.post(url, headers=headers, json=submission)
            
            if response.status_code in [200, 201]:
                results["successful"] += 1
                print(f"✓ Record {i}/{len(records)} uploaded successfully")
            else:
                results["failed"] += 1
                error_msg = f"Record {i}: {response.status_code} - {response.text}"
                results["errors"].append(error_msg)
                print(f"✗ {error_msg}")
                
        except Exception as e:
            results["failed"] += 1
            error_msg = f"Record {i}: {str(e)}"
            results["errors"].append(error_msg)
            print(f"✗ {error_msg}")
    
    return results

def save_to_json(records, filename="tool_rental_sample_data.json"):
    """Save sample data to JSON file"""
    with open(filename, 'w') as f:
        json.dump(records, f, indent=2)
    print(f"\nSample data saved to {filename}")

def save_to_csv(records, filename="tool_rental_sample_data.csv"):
    """Save sample data to CSV file"""
    import csv
    
    if not records:
        return
    
    with open(filename, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
    print(f"Sample data saved to {filename}")

def test_kobo_connection():
    """Test connection to Kobo Toolbox API"""
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
    }
    
    try:
        response = requests.get(f"{KOBO_BASE_URL}/assets/", headers=headers)
        if response.status_code == 200:
            print("✓ Successfully connected to Kobo Toolbox API")
            assets = response.json()
            print(f"\nFound {len(assets.get('results', []))} forms/assets:")
            for asset in assets.get('results', [])[:5]:
                print(f"  - {asset.get('name')} (ID: {asset.get('uid')})")
            return True
        else:
            print(f"✗ Connection failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"✗ Error connecting to Kobo: {str(e)}")
        return False

def main():
    print("=" * 60)
    print("Tool Rental Trial Program - Kobo Toolbox Upload")
    print("=" * 60)
    
    # Test API connection
    print("\n1. Testing Kobo Toolbox API connection...")
    if not test_kobo_connection():
        print("\nPlease verify your API key and try again.")
        return
    
    # Generate sample data
    print("\n2. Generating sample data...")
    num_records = 30
    records = generate_sample_data(num_records)
    print(f"✓ Generated {len(records)} sample records")
    
    # Save to files
    print("\n3. Saving sample data to files...")
    save_to_json(records)
    save_to_csv(records)
    
    # Display sample records
    print("\n4. Sample Records Preview:")
    print("-" * 60)
    for i, record in enumerate(records[:3], 1):
        print(f"\nRecord {i}:")
        for key, value in record.items():
            print(f"  {key}: {value}")
    print(f"\n... and {len(records) - 3} more records")
    
    # Upload to Kobo
    print("\n5. Upload to Kobo Toolbox:")
    print("-" * 60)
    
    if KOBO_ASSET_ID != "YOUR_FORM_ID_HERE":
        upload_choice = input("\nDo you want to upload to Kobo now? (yes/no): ")
        if upload_choice.lower() == 'yes':
            results = upload_to_kobo(records, KOBO_ASSET_ID)
            print("\n" + "=" * 60)
            print("Upload Results:")
            print(f"  Successful: {results['successful']}")
            print(f"  Failed: {results['failed']}")
            if results['errors']:
                print("\nErrors:")
                for error in results['errors'][:5]:
                    print(f"  - {error}")
        else:
            print("Upload skipped.")
    else:
        print("\nNOTE: Please set KOBO_ASSET_ID to your form ID to enable upload.")
        print("The script will now display your available forms...")
    
    print("\n" + "=" * 60)
    print("Script completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    main()

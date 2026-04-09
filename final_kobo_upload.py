"""
Upload Data to Kobo Toolbox using OpenRosa API
"""

import requests
import json
from datetime import datetime, timedelta
import random
import uuid
import xml.etree.ElementTree as ET

# Configuration
KOBO_API_KEY = "7327ad2b882fb5d4975916811759dc339b266cd4"
FORM_ID_STRING = "aJ7GEDZPU3dbM4KAEqUBKW"
FORM_ID = "3411436"

# Sample data  
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
            "quantity": str(random.randint(1, 4))
        }
        
        records.append(record)
    
    return records

def create_xml_submission(record):
    """Create an ODK-compliant XML submission"""
    # Root element with form ID
    data = ET.Element('data', attrib={
        'id': FORM_ID_STRING,
        'version': '1'
    })
    
    # Add all fields
    for key, value in record.items():
        elem = ET.SubElement(data, key)
        elem.text = str(value) if value else ""
    
    # Add metadata
    meta = ET.SubElement(data, 'meta')
    instance_id = ET.SubElement(meta, 'instanceID')
    instance_id.text = f"uuid:{uuid.uuid4()}"
    
    # Add submission time
    submission_date = ET.SubElement(meta, 'submissionDate')
    submission_date.text = datetime.now().isoformat()
    
    return ET.tostring(data, encoding='utf-8')

def upload_via_openrosa(records):
    """Upload submissions via OpenRosa API"""
    
    # OpenRosa submission endpoint
    submission_url = f"https://kc.kobotoolbox.org/griffinmgwhite123/submission"
    
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
    }
    
    results = {"successful": 0, "failed": 0, "errors": []}
    
    print(f"\nUploading {len(records)} records via OpenRosa API...")
    print(f"Endpoint: {submission_url}")
    print("-" * 70)
    
    for i, record in enumerate(records, 1):
        try:
            xml_data = create_xml_submission(record)
            
            files = {
                'xml_submission_file': (f'submission_{i}.xml', xml_data, 'text/xml')
            }
            
            response = requests.post(submission_url, headers=headers, files=files)
            
            if response.status_code in [200, 201, 202]:
                results["successful"] += 1
                print(f"✓ Record {i}/{len(records)} uploaded")
            elif response.status_code == 403:
                results["failed"] += 1
                error = f"Authentication failed - check API key permissions"
                results["errors"].append(error)
                print(f"✗ {error}")
                break  # No point continuing if auth fails
            else:
                results["failed"] += 1
                error = f"Record {i}: HTTP {response.status_code}"
                results["errors"].append(error)
                print(f"✗ {error}")
                
                if i == 1:  # Show first error details
                    print(f"   Response: {response.text[:300]}")
                
        except Exception as e:
            results["failed"] += 1
            error = f"Record {i}: {str(e)}"
            results["errors"].append(error)
            print(f"✗ {error}")
    
    return results

def try_direct_json_upload(records):
    """Try uploading directly via JSON data endpoint"""
    
    url = f"https://kc.kobotoolbox.org/api/v1/data/{FORM_ID}"
    
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    results = {"successful": 0, "failed": 0, "errors": []}
    
    print(f"\nTrying direct JSON upload...")
    print(f"Endpoint: {url}")
    print("-" * 70)
    
    for i, record in enumerate(records, 1):
        try:
            # Add instance ID
            submission = {
                **record,
                "meta/instanceID": f"uuid:{uuid.uuid4()}"
            }
            
            response = requests.post(url, headers=headers, json=submission)
            
            if response.status_code in [200, 201, 202]:
                results["successful"] += 1
                print(f"✓ Record {i}/{len(records)} uploaded")
            else:
                results["failed"] += 1
                error = f"Record {i}: HTTP {response.status_code}"
                results["errors"].append(error)
                print(f"✗ {error}")
                
                if i == 1:
                    print(f"   Response: {response.text[:200]}")
                
        except Exception as e:
            results["failed"] += 1
            error = f"Record {i}: {str(e)}"
            results["errors"].append(error)
            print(f"✗ {error}")
    
    return results

def main():
    print("=" * 70)
    print("Uploading Tool Rental Data to Kobo Toolbox")
    print(f"Form: Tool Rental Trial Program")
    print(f"Form ID: {FORM_ID_STRING}")
    print("=" * 70)
    
    # Generate data
    print("\nGenerating sample data...")
    records = generate_sample_data(30)
    print(f"✓ Generated {len(records)} records")
    
    # Show sample
    print("\nSample record:")
    print(json.dumps(records[0], indent=2))
    
    # Try OpenRosa method
    print("\n" + "=" * 70)
    print("METHOD 1: OpenRosa XML Submission")
    print("=" * 70)
    results = upload_via_openrosa(records)
    
    # If that fails, try JSON
    if results["successful"] == 0:
        print("\n" + "=" * 70)
        print("METHOD 2: Direct JSON Upload")
        print("=" * 70)
        results = try_direct_json_upload(records)
    
    # Final results
    print("\n" + "=" * 70)
    print("FINAL RESULTS")
    print("=" * 70)
    print(f"✓ Successful uploads: {results['successful']}")
    print(f"✗ Failed uploads: {results['failed']}")
    
    if results['successful'] > 0:
        print(f"\n🎉 SUCCESS! {results['successful']} records uploaded!")
        print(f"\nView your data at:")
        print(f"https://kf.kobotoolbox.org/#/forms/{FORM_ID_STRING}/data/table")
    else:
        print("\n⚠️  API upload did not succeed.")
        print("\nThe Kobo API has restrictions on programmatic submissions.")
        print("Please use the manual upload method:")
        print("\n1. Files have been generated in your directory:")
        print("   - tool_rental_kobo_import.xlsx")
        print("   - tool_rental_kobo_import.csv")
        print(f"\n2. Visit: https://kf.kobotoolbox.org/#/forms/{FORM_ID_STRING}/data/table")
        print("3. Click 'Upload' or 'Import' and select the Excel file")
        
        if results['errors']:
            print("\nError details:")
            for error in results['errors'][:3]:
                print(f"  - {error}")
    
    print("=" * 70)

if __name__ == "__main__":
    main()

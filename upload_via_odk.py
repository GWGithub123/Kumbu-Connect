"""
Upload to Kobo via ODK XML Submission
"""

import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import random
import uuid

# Configuration
KOBO_API_KEY = "7327ad2b882fb5d4975916811759dc339b266cd4"
KOBO_ASSET_ID = "aJ7GEDZPU3dbM4KAEqUBKW"

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

def get_submission_url():
    """Get the ODK submission URL"""
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
    }
    
    response = requests.get(
        f"https://kf.kobotoolbox.org/api/v2/assets/{KOBO_ASSET_ID}/deployment/",
        headers=headers
    )
    
    if response.status_code == 200:
        data = response.json()
        # The form URL might be in the deployment
        return data.get('submission_url') or f"https://kc.kobotoolbox.org/griffinmgwhite123/submission"
    return None

def create_xml_submission(record, form_id="tool_rental_trial"):
    """Create an ODK XML submission"""
    data = ET.Element('data', attrib={'id': form_id})
    
    for key, value in record.items():
        elem = ET.SubElement(data, key)
        elem.text = str(value)
    
    # Add metadata
    meta = ET.SubElement(data, 'meta')
    instance_id = ET.SubElement(meta, 'instanceID')
    instance_id.text = f"uuid:{uuid.uuid4()}"
    
    return ET.tostring(data, encoding='unicode')

def upload_via_odk(records):
    """Upload submissions via ODK XML"""
    submission_url = get_submission_url()
    
    if not submission_url:
        print("Could not get submission URL")
        return {"successful": 0, "failed": len(records), "errors": ["No submission URL"]}
    
    print(f"Using submission URL: {submission_url}")
    
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
    }
    
    results = {"successful": 0, "failed": 0, "errors": []}
    
    for i, record in enumerate(records, 1):
        try:
            xml_data = create_xml_submission(record)
            
            files = {
                'xml_submission_file': ('submission.xml', xml_data, 'text/xml')
            }
            
            response = requests.post(submission_url, headers=headers, files=files)
            
            if response.status_code in [200, 201, 202]:
                results["successful"] += 1
                print(f"✓ Record {i}/{len(records)} uploaded")
            else:
                results["failed"] += 1
                error = f"Record {i}: {response.status_code}"
                results["errors"].append(error)
                print(f"✗ {error}")
                if i == 1:  # Print first error for debugging
                    print(f"Response: {response.text[:200]}")
                
        except Exception as e:
            results["failed"] += 1
            error = f"Record {i}: {str(e)}"
            results["errors"].append(error)
            print(f"✗ {error}")
    
    return results

def main():
    print("=" * 70)
    print("Attempting ODK XML Upload to Kobo")
    print("=" * 70)
    
    # Generate data
    print("\nGenerating sample data...")
    records = generate_sample_data(30)
    print(f"✓ Generated {len(records)} records")
    
    # Try upload
    print("\nAttempting upload via ODK XML...")
    print("-" * 70)
    results = upload_via_odk(records)
    
    # Results
    print("\n" + "=" * 70)
    print("Results:")
    print(f"  ✓ Successful: {results['successful']}")
    print(f"  ✗ Failed: {results['failed']}")
    
    if results['successful'] > 0:
        print(f"\n✓ SUCCESS! {results['successful']} records uploaded to Kobo!")
        print(f"View at: https://kf.kobotoolbox.org/#/forms/{KOBO_ASSET_ID}/data/table")
    else:
        print("\n✗ ODK upload did not work. Please use the manual upload method:")
        print("   1. Use the files generated by generate_kobo_data_for_upload.py")
        print("   2. Upload tool_rental_kobo_import.xlsx via the Kobo web interface")
    
    print("=" * 70)

if __name__ == "__main__":
    main()

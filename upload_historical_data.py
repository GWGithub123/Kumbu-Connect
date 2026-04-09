"""
Generate and upload 6 months of historical tool rental data to KoboToolbox.
Creates realistic growth patterns to demonstrate social impact metrics over time.
Uses OpenRosa XML submission API.
"""
import requests
import json
import random
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

# Configuration
KOBO_API_KEY = "7327ad2b882fb5d4975916811759dc339b266cd4"
KOBO_FORM_ID = "aJ7GEDZPU3dbM4KAEqUBKW"
KOBO_SUBMISSION_URL = "https://kc.kobotoolbox.org/submission"

TOOLS = [
    "Hammer", "Drill", "Saw", "Screwdriver Set", "Wrench Set",
    "Pliers", "Level", "Tape Measure", "Ladder", "Power Drill",
    "Circular Saw", "Sander", "Paint Roller", "Garden Hose", "Wheelbarrow",
    "Rake", "Shovel", "Machete", "Pickaxe", "Hoe"
]

BORROWERS = [
    "Jesse Artache", "Isaac Simkin", "Israel Simmons", "Lydia Anwor",
    "Danek Obriondo", "Derek Obriondo", "Clement Jackson", "Cassanova Miller",
    "Maria Rodriguez", "John Thompson", "Sarah Williams", "Michael Chen",
    "Patricia Davis", "James Omondi", "Grace Wanjiru", "Peter Kamau",
    "Agnes Nyambura", "Daniel Kipchoge", "Ruth Akinyi", "Samuel Mwangi"
]

CONDITIONS = [
    "Good condition upon return", "Good Condition upon Return",
    "Excellent condition", "Minor wear, acceptable", "Needs cleaning",
    "Small scratch noted", "Well maintained"
]

DAMAGE_CHARGES = ["—", "—", "—", "—", "—", "$5 cleaning fee", "$10 minor damage", "$15 repair needed", "No charge"]


def generate_historical_data(num_months=6):
    """Generate growing rental data over 6 months."""
    records = []
    end_date = datetime.now()
    
    # Start from 6 months ago
    current_date = end_date - timedelta(days=30 * num_months)
    
    # Growth parameters - start small, grow each month
    base_rentals_per_month = 10
    growth_rate = 1.4  # 40% month-over-month growth
    
    serial_counter = 2000
    
    for month in range(num_months):
        # Calculate rentals for this month (growing pattern)
        rentals_this_month = int(base_rentals_per_month * (growth_rate ** month))
        
        # Add growing borrower pool
        active_borrowers = BORROWERS[:min(5 + month * 3, len(BORROWERS))]
        
        for i in range(rentals_this_month):
            # Random loan date within this month
            days_into_month = random.randint(0, 28)
            loan_date = current_date + timedelta(days=days_into_month)
            loan_time = f"{random.randint(7, 17):02d}:{random.choice(['00', '15', '30', '45'])}"
            
            # Rental duration (slightly shorter in early months)
            max_duration = min(3 + month, 10)
            days_borrowed = random.randint(1, max_duration)
            return_date = loan_date + timedelta(days=days_borrowed)
            return_time = f"{random.randint(7, 17):02d}:{random.choice(['00', '15', '30', '45'])}"
            
            record = {
                "cbo_identifier": "tools",  # Original tool-sharing CBO
                "date_loaned": loan_date.strftime("%Y-%m-%d"),
                "time_loaned": loan_time,
                "date_returned": return_date.strftime("%Y-%m-%d"),
                "time_returned": return_time,
                "tool_name": random.choice(TOOLS),
                "borrower_name": random.choice(active_borrowers),
                "borrower_signature": random.choice(active_borrowers).split()[0],
                "condition_upon_return": random.choice(CONDITIONS),
                "damage_charged": random.choice(DAMAGE_CHARGES),
                "return_notes": random.choice([
                    "On time return", "Late return, no charge", "Returned clean",
                    "Needs inspection", "Good borrower", "Repeat customer", ""
                ]),
                "serial_number": f"TOOL-{serial_counter}",
                "quantity": str(random.randint(1, 3))
            }
            
            records.append(record)
            serial_counter += 1
        
        # Move to next month
        current_date += timedelta(days=30)
    
    return records


def upload_submission(record):
    """Upload a single submission to KoboToolbox using OpenRosa XML format."""
    
    # Build XML submission
    root = ET.Element("data", id=KOBO_FORM_ID)
    
    for key, value in record.items():
        elem = ET.SubElement(root, key)
        elem.text = str(value)
    
    # Add meta information
    meta = ET.SubElement(root, "meta")
    instanceID = ET.SubElement(meta, "instanceID")
    instanceID.text = f"uuid:{record['serial_number']}-{random.randint(1000,9999)}"
    
    xml_data = ET.tostring(root, encoding='utf-8', method='xml')
    
    headers = {
        "Authorization": f"Token {KOBO_API_KEY}",
    }
    
    files = {
        'xml_submission_file': ('submission.xml', xml_data, 'text/xml')
    }
    
    try:
        response = requests.post(KOBO_SUBMISSION_URL, headers=headers, files=files, timeout=10)
        if response.status_code in (200, 201, 202):
            return True
        else:
            print(f"  ❌ Failed: {response.status_code} - {response.text[:100]}")
            return False
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False


def main():
    print("=" * 70)
    print("Historical Tool Rental Data Upload to KoboToolbox")
    print("=" * 70)
    
    # Generate 6 months of data
    print("\nGenerating 6 months of historical rental data with growth patterns...")
    records = generate_historical_data(num_months=6)
    print(f"✓ Generated {len(records)} rental records")
    
    # Show growth summary
    from collections import defaultdict
    by_month = defaultdict(int)
    for r in records:
        month_key = r['date_loaned'][:7]  # YYYY-MM
        by_month[month_key] += 1
    
    print("\n📈 Growth Pattern:")
    for month in sorted(by_month.keys()):
        print(f"   {month}: {by_month[month]} rentals")
    
    # Upload
    print(f"\nUploading {len(records)} records to KoboToolbox...")
    successful = 0
    failed = 0
    
    for i, record in enumerate(records, 1):
        if i % 10 == 0:
            print(f"  Progress: {i}/{len(records)} ({int(100*i/len(records))}%)")
        
        if upload_submission(record):
            successful += 1
        else:
            failed += 1
            if failed >= 5:  # Stop after 5 failures to debug
                print("\n⚠ Stopping after 5 failures. Check the error messages above.")
                break
    
    print("\n" + "=" * 70)
    print(f"✅ Upload Complete!")
    print(f"   Successful: {successful}")
    print(f"   Failed: {failed}")
    print("=" * 70)


if __name__ == "__main__":
    main()

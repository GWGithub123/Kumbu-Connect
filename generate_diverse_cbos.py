"""
Generate and upload data for 4 diverse CBOs to KoboToolbox.
Creates realistic operational data for education, healthcare, agriculture, and water/sanitation CBOs.
"""
import requests
import random
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

# Configuration
KOBO_API_KEY = "7327ad2b882fb5d4975916811759dc339b266cd4"
KOBO_FORM_ID = "aJ7GEDZPU3dbM4KAEqUBKW"
KOBO_SUBMISSION_URL = "https://kc.kobotoolbox.org/submission"

# ============================================================================
# CBO 1: EDUCATION CBO - Tutoring & Learning Materials Program
# ============================================================================
EDUCATION_ITEMS = [
    "Math Textbook", "Science Workbook", "English Dictionary", "Calculator",
    "Study Desk", "Reading Lamp", "Computer for Homework", "Tablet Device",
    "Tutoring Session (2hr)", "Group Study Room", "Exam Prep Materials",
    "Writing Workshop", "STEM Kit", "Library Card Access", "Online Course Access"
]

EDUCATION_BORROWERS = [
    "Amina Hassan", "David Kiprop", "Grace Njeri", "Samuel Odhiambo",
    "Faith Wanjiku", "Kevin Mutua", "Lucy Achieng", "Peter Kamande",
    "Rebecca Mwikali", "Joseph Kimani", "Eunice Nyambura", "Brian Otieno",
    "Catherine Wairimu", "Dennis Karanja", "Mary Adhiambo"
]

# ============================================================================
# CBO 2: HEALTHCARE CBO - Medical Supplies & Wellness Program
# ============================================================================
HEALTHCARE_ITEMS = [
    "First Aid Kit", "Blood Pressure Monitor", "Thermometer", "Wheelchair",
    "Crutches", "Glucose Monitor", "Nebulizer", "Oxygen Concentrator",
    "Health Screening", "Nutrition Consultation", "Mental Health Session",
    "Prenatal Care Visit", "Vaccination Drive", "Eye Exam Kit", "Dental Checkup"
]

HEALTHCARE_BORROWERS = [
    "Jane Muthoni", "John Omondi", "Sarah Chebet", "Michael Wanjala",
    "Patricia Auma", "Daniel Kipchoge", "Agnes Wambui", "Simon Mutiso",
    "Ruth Awuor", "Francis Kiprotich", "Elizabeth Njoki", "George Onyango",
    "Nancy Wangari", "Paul Kimutai", "Helen Akinyi"
]

# ============================================================================
# CBO 3: AGRICULTURE CBO - Seeds, Fertilizer & Equipment Program
# ============================================================================
AGRICULTURE_ITEMS = [
    "Maize Seeds (5kg)", "Bean Seeds (2kg)", "Fertilizer (10kg)", "Pesticide Spray",
    "Hand Tiller", "Water Pump", "Irrigation Hose", "Greenhouse Film",
    "Pruning Shears", "Wheelbarrow", "Spade", "Hoe", "Rake",
    "Training: Crop Rotation", "Training: Organic Farming", "Soil Testing Kit"
]

AGRICULTURE_BORROWERS = [
    "James Karanja", "Rose Wanjiru", "Thomas Kiplagat", "Alice Nyokabi",
    "William Kibet", "Margaret Mumbua", "Charles Ochieng", "Susan Waithera",
    "Daniel Koech", "Florence Njambi", "Peter Rotich", "Jane Wambui",
    "David Kiptoo", "Mary Wangui", "Stephen Kiprono"
]

# ============================================================================
# CBO 4: WATER & SANITATION CBO - Clean Water & Hygiene Program
# ============================================================================
WATER_ITEMS = [
    "Water Filter", "20L Water Jerry Can", "Water Testing Kit", "Hand Pump Repair",
    "Latrine Construction", "Hand Washing Station", "Soap Distribution",
    "Hygiene Training", "Borehole Maintenance", "Rainwater Harvesting Tank",
    "Water Storage Tank (500L)", "PVC Pipes (10m)", "Tap Installation",
    "Community Water Point", "Water Quality Testing"
]

WATER_BORROWERS = [
    "Joseph Macharia", "Anne Njoroge", "Patrick Mwangi", "Lucy Wanjiku",
    "Robert Kimani", "Grace Nyambura", "Samuel Gitau", "Esther Wangari",
    "Francis Kamau", "Betty Njeri", "Anthony Kuria", "Martha Wambui",
    "Isaac Maina", "Purity Mwangi", "Joshua Kamande"
]

CONDITION_STATES = [
    "Good condition upon return", "Excellent condition", "Minor wear, acceptable",
    "Needs cleaning", "Well maintained", "Returned in good state"
]

DAMAGE_CHARGES = ["—", "—", "—", "—", "—", "$5 cleaning", "$10 minor repair", "$15 damage", "No charge"]


def generate_cbo_data(cbo_type, items, borrowers, num_months=6, base_count=12):
    """Generate realistic operational data for a specific CBO type."""
    records = []
    end_date = datetime.now()
    current_date = end_date - timedelta(days=30 * num_months)
    
    growth_rate = 1.35  # 35% month-over-month growth
    serial_counter = random.randint(5000, 6000)
    
    for month in range(num_months):
        # Growing activity each month
        activities_this_month = int(base_count * (growth_rate ** month))
        active_borrowers = borrowers[:min(5 + month * 2, len(borrowers))]
        
        for i in range(activities_this_month):
            days_into_month = random.randint(0, 28)
            loan_date = current_date + timedelta(days=days_into_month)
            loan_time = f"{random.randint(7, 17):02d}:{random.choice(['00', '15', '30', '45'])}"
            
            # Duration varies by CBO type
            if cbo_type == "education":
                days_duration = random.randint(7, 21)  # Week to 3 weeks
            elif cbo_type == "healthcare":
                days_duration = random.randint(1, 7)  # 1 day to 1 week
            elif cbo_type == "agriculture":
                days_duration = random.randint(14, 90)  # 2 weeks to 3 months (growing season)
            else:  # water
                days_duration = random.randint(1, 30)  # 1 day to 1 month
            
            return_date = loan_date + timedelta(days=days_duration)
            return_time = f"{random.randint(7, 17):02d}:{random.choice(['00', '15', '30', '45'])}"
            
            record = {
                "cbo_identifier": cbo_type,  # Add CBO identifier to filter data
                "date_loaned": loan_date.strftime("%Y-%m-%d"),
                "time_loaned": loan_time,
                "date_returned": return_date.strftime("%Y-%m-%d"),
                "time_returned": return_time,
                "tool_name": random.choice(items),
                "borrower_name": random.choice(active_borrowers),
                "borrower_signature": random.choice(active_borrowers).split()[0],
                "condition_upon_return": random.choice(CONDITION_STATES),
                "damage_charged": random.choice(DAMAGE_CHARGES),
                "return_notes": random.choice([
                    "Timely return", "Good beneficiary", "Repeat participant",
                    "Positive feedback", "Excellent condition", "No issues", ""
                ]),
                "serial_number": f"{cbo_type.upper()[:3]}-{serial_counter}",
                "quantity": str(random.randint(1, 3))
            }
            
            records.append(record)
            serial_counter += 1
        
        current_date += timedelta(days=30)
    
    return records


def upload_submission(record):
    """Upload a single submission to KoboToolbox using OpenRosa XML format."""
    root = ET.Element("data", id=KOBO_FORM_ID)
    
    for key, value in record.items():
        elem = ET.SubElement(root, key)
        elem.text = str(value)
    
    # Add meta information
    meta = ET.SubElement(root, "meta")
    instanceID = ET.SubElement(meta, "instanceID")
    instanceID.text = f"uuid:{record['serial_number']}-{random.randint(1000,9999)}"
    
    xml_data = ET.tostring(root, encoding='utf-8', method='xml')
    
    headers = {"Authorization": f"Token {KOBO_API_KEY}"}
    files = {'xml_submission_file': ('submission.xml', xml_data, 'text/xml')}
    
    try:
        response = requests.post(KOBO_SUBMISSION_URL, headers=headers, files=files, timeout=10)
        return response.status_code in (200, 201, 202)
    except:
        return False


def main():
    print("=" * 80)
    print("DIVERSE CBO DATA GENERATION FOR KOBO TOOLBOX")
    print("=" * 80)
    
    cbos = [
        {
            "name": "Bright Futures Education CBO",
            "type": "education",
            "items": EDUCATION_ITEMS,
            "borrowers": EDUCATION_BORROWERS,
            "base_count": 10
        },
        {
            "name": "Health For All CBO",
            "type": "healthcare",
            "items": HEALTHCARE_ITEMS,
            "borrowers": HEALTHCARE_BORROWERS,
            "base_count": 15
        },
        {
            "name": "Green Harvest Agriculture CBO",
            "type": "agriculture",
            "items": AGRICULTURE_ITEMS,
            "borrowers": AGRICULTURE_BORROWERS,
            "base_count": 12
        },
        {
            "name": "Clean Water Initiative CBO",
            "type": "water",
            "items": WATER_ITEMS,
            "borrowers": WATER_BORROWERS,
            "base_count": 8
        }
    ]
    
    total_uploaded = 0
    total_failed = 0
    
    for cbo in cbos:
        print(f"\n{'=' * 80}")
        print(f"📊 {cbo['name'].upper()}")
        print(f"{'=' * 80}")
        
        # Generate data
        print(f"Generating 6 months of {cbo['type']} program data...")
        records = generate_cbo_data(
            cbo['type'], 
            cbo['items'], 
            cbo['borrowers'],
            num_months=6,
            base_count=cbo['base_count']
        )
        print(f"✓ Generated {len(records)} activity records")
        
        # Show monthly breakdown
        from collections import defaultdict
        by_month = defaultdict(int)
        for r in records:
            month_key = r['date_loaned'][:7]
            by_month[month_key] += 1
        
        print(f"\n📈 Monthly Growth Pattern:")
        for month in sorted(by_month.keys()):
            print(f"   {month}: {by_month[month]} activities")
        
        # Upload
        print(f"\nUploading {len(records)} records...")
        successful = 0
        failed = 0
        consecutive_fails = 0
        
        for i, record in enumerate(records, 1):
            if i % 20 == 0:
                print(f"  Progress: {i}/{len(records)} ({int(100*i/len(records))}%) - Success: {successful}, Failed: {failed}")
            
            if upload_submission(record):
                successful += 1
                consecutive_fails = 0
            else:
                failed += 1
                consecutive_fails += 1
                
                # Stop if too many consecutive failures (rate limiting)
                if consecutive_fails >= 5:
                    print(f"\n  ⚠️  Pausing after {consecutive_fails} consecutive failures (rate limit)")
                    import time
                    time.sleep(2)
                    consecutive_fails = 0
        
        print(f"\n✅ {cbo['name']}: {successful} uploaded, {failed} failed")
        total_uploaded += successful
        total_failed += failed
    
    print("\n" + "=" * 80)
    print("📊 FINAL SUMMARY")
    print("=" * 80)
    print(f"✅ Total Records Uploaded: {total_uploaded}")
    print(f"❌ Total Failed: {total_failed}")
    print(f"📈 Success Rate: {int(100*total_uploaded/(total_uploaded+total_failed))}%")
    print("\n🎉 All 4 CBOs now have data in KoboToolbox!")
    print("   Next: Sync your application to pull this data and populate the marketplace")
    print("=" * 80)


if __name__ == "__main__":
    main()

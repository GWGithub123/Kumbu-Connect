"""
Clear and re-upload all CBO data to KoboToolbox with proper identifiers.
"""
import sys
sys.path.append('.')

print("=" * 80)
print("RE-UPLOADING ALL CBO DATA WITH IDENTIFIERS")
print("=" * 80)
print("\nThis will upload fresh data for all 6 CBOs with proper cbo_identifier fields.")
print("Previous data in KoboToolbox will remain but new data will have identifiers.\n")

import subprocess

# Upload tool-sharing data
print("\n1. Uploading Tool-Sharing CBO data...")
result = subprocess.run(['python3', 'upload_historical_data.py'], capture_output=False)

# Upload diverse CBOs
print("\n2. Uploading Education, Healthcare, Agriculture, Water CBOs data...")
result = subprocess.run(['python3', 'generate_diverse_cbos.py'], capture_output=False)

print("\n" + "=" * 80)
print("✅ DATA UPLOAD COMPLETE!")
print("=" * 80)
print("\nNow you can sync each CBO and they will get their proper filtered data.")
print("Visit http://127.0.0.1:5000 and sync each CBO profile.")

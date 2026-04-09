# Separate KoboToolbox Forms Setup Guide

## Current Status
- **Tool Sharing CBOs** (Busia & Kakamega): Using existing form `aJ7GEDZPU3dbM4KAEqUBKW` ✅
- **Education CBO**: Needs its own form ⚠️
- **Healthcare CBO**: Needs its own form ⚠️
- **Agriculture CBO**: Needs its own form ⚠️
- **Water/Sanitation CBO**: Needs its own form ⚠️

## Steps to Create Forms

### Option 1: Duplicate the Existing Form (Recommended)
1. Go to KoboToolbox: https://kf.kobotoolbox.org/
2. Find your "Tool Rental Trial Program" form
3. Click the three dots (⋮) → "Clone this project"
4. Rename each cloned form:
   - "Education Program Tracking"
   - "Healthcare Services Tracking"
   - "Agriculture Program Tracking"
   - "Water & Sanitation Tracking"
5. For each form, deploy it and get the Asset ID

### Option 2: Use the Same Form for All (Simplest)
Just keep using `aJ7GEDZPU3dbM4KAEqUBKW` for all CBOs. Since the data structure is similar (date loaned, date returned, item name, borrower, etc.), you can use the same form. The system will show different interpretations based on the CBO type.

## How to Get the Asset ID

Run this command to see all your forms:
```bash
curl -s -H "Authorization: Token 7327ad2b882fb5d4975916811759dc339b266cd4" \
  "https://kc.kobotoolbox.org/api/v1/forms" | \
  python3 -c "import sys, json; forms = json.load(sys.stdin); \
  print('\\n'.join([f\"{f['id_string']} - {f['title']} (ID: {f['formid']})\" for f in forms]))"
```

## Update the Database

Once you have the form IDs, update `seed.py`:

```python
# Education CBO
kobo_asset_id="YOUR_EDUCATION_FORM_ID",

# Healthcare CBO
kobo_asset_id="YOUR_HEALTHCARE_FORM_ID",

# Agriculture CBO
kobo_asset_id="YOUR_AGRICULTURE_FORM_ID",

# Water CBO
kobo_asset_id="YOUR_WATER_FORM_ID",
```

Then reseed the database:
```bash
rm -f webapp/instance/kumbu.db && python3 seed.py
```

## Quick Test (Using Same Form for All)

If you want to test quickly, just use the existing form for all CBOs:

```bash
python3 -c "
from webapp import create_app
from webapp.models import db, CBO

app = create_app()
with app.app_context():
    # Update all CBOs to use the same form
    for cbo in CBO.query.all():
        cbo.kobo_asset_id = 'aJ7GEDZPU3dbM4KAEqUBKW'
    db.session.commit()
    print('✅ All CBOs now use the tool rental form')
    print('   The AI will interpret the data based on each CBO type')
"
```

Then sync all CBOs:
```bash
python3 sync_all_cbos.py
```

## Recommendation

**For now, use the same form for all CBOs.** The system is smart enough to:
- Detect the CBO type from `cbo_identifier`
- Interpret "tool_name" as appropriate (textbook, medical supply, seeds, water filter)
- Calculate sector-specific metrics
- Generate context-appropriate profiles

Later, you can create separate forms with sector-specific field names if needed.

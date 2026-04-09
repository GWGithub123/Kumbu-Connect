# Community Feedback SMS Setup

This project now includes a lightweight SMS-driven community feedback workflow built for CBO data collection.

## What It Does

- Community members text a CBO keyword to your Twilio number.
- The app enrolls their phone number under that CBO.
- The SMS flow collects:
  - a 1 to 10 impact rating
  - an optional count of how many times the CBO helped them
  - a short anecdotal story
- Responses are saved locally in the Flask database.
- If Firebase credentials are configured, the same subscriber and feedback records are mirrored into Firestore under:
  - `cbos/{cbo_slug}/community_subscribers/{subscriber_id}`
  - `cbos/{cbo_slug}/community_feedback/{feedback_id}`

## Important Security Note

Twilio credentials should only live in environment variables.

If any real Twilio auth token or SID has been shared in plaintext, rotate it in the Twilio console before deploying this workflow.

## Environment Variables

Copy values into `.env` from `.env.example`:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER`
- `TWILIO_VALIDATE_SIGNATURE`
- `COMMUNITY_FEEDBACK_CHECKIN_MONTHS`
- `FIREBASE_PROJECT_ID`
- `FIREBASE_SERVICE_ACCOUNT_JSON`

## Database Schema

New tables are created automatically on app start:

- `community_subscribers`
- `community_feedback`

The `cbos` table also now supports:

- `sms_keyword`
- `community_prompt`
- `community_feedback_enabled`

To activate a CBO keyword, set `sms_keyword` for that CBO record.

## Twilio Webhook

Configure your Twilio phone number to send inbound messages to:

- `/sms/webhook`

For local testing, expose the Flask app through a public tunnel.

Example local run:

```bash
python run.py
```

Then point Twilio at your public URL, for example:

```text
https://your-public-url.example/sms/webhook
```

Set `TWILIO_VALIDATE_SIGNATURE=true` in production.

## Scheduled Re-Engagement

To send the next round of check-in prompts to subscribers who are due:

```bash
python send_community_checkins.py
```

This script is intended to be run by cron, Cloud Run jobs, or Cloud Scheduler.

## Admin Pages And Local Testing

Once the app is running, you can inspect and test the SMS workflow directly in the browser.

Funder accounts can use:

- `/admin/community-feedback`
- `/admin/community-feedback/<cbo_id>`

The CBO detail page includes:

- SMS keyword and prompt settings
- subscriber and response tables
- stored message transcript history
- a local SMS simulator

Recommended simulator flow:

1. Submit the CBO keyword from a test phone number.
2. Submit a rating from 1 to 10.
3. Submit the number of times the CBO helped, or `SKIP`.
4. Submit a short anecdote.

The page will show the latest system reply after each simulated message.

## Firebase Hosting And Firestore

This repo still runs as a Flask app, but the data layer is now compatible with a Firebase-backed deployment plan:

- Host the app on a public server first so Twilio can reach the webhook.
- Mirror community data to Firestore using a Firebase service account.
- Move the same webhook logic behind your cloud deployment later without changing the SMS flow.

## Suggested Next Steps

1. Add `sms_keyword` values for each existing CBO.
2. Rotate any exposed Twilio credentials.
3. Install dependencies with `pip install -r requirements.txt`.
4. Set the Twilio webhook URL.
5. Run a live end-to-end test from a real phone.
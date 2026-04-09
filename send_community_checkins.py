"""Send scheduled SMS feedback prompts to community subscribers who are due."""
from webapp import create_app
from webapp.community_feedback import send_due_checkins


app = create_app()


if __name__ == '__main__':
    with app.app_context():
        result = send_due_checkins()
        print(
            f"Sent {result['sent']} check-ins, skipped {result['skipped']}, evaluated {result['total_due']} active subscribers."
        )
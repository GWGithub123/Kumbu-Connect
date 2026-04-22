"""
run.py — Entry-point for the Kumbu Connect web application.

    python run.py
"""
from webapp import create_app

app = create_app()

if __name__ == '__main__':
    app.run(
        debug=True,
        host=app.config.get('APP_HOST', '127.0.0.1'),
        port=app.config.get('APP_PORT', 8000),
    )

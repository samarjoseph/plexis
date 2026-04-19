from backend.app import app

# This is what Gunicorn will use
application = app

if __name__ == "__main__":
    app.run()
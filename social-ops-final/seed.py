"""Create the first boss account. Run once: python seed.py"""
from app import create_app
from models import User, db

app = create_app()

with app.app_context():
    if User.query.filter_by(role="boss").first():
        print("A boss account already exists — skipping.")
    else:
        name = input("Your name: ").strip() or "Boss"
        email = input("Your email: ").strip().lower()
        password = input("Choose a password: ").strip()
        u = User(name=name, email=email, role="boss")
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        print(f"Created boss account for {email}. Log in at /login.")

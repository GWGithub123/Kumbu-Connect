"""
Authentication blueprint — dual login for Funders & CBOs.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from .models import db, User, CBO
import re

auth_bp = Blueprint('auth', __name__)


# ── helpers ───────────────────────────────────────────────────────
def _slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


# ── Login ─────────────────────────────────────────────────────────
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return _redirect_by_role(current_user)

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        role = request.form.get('role', 'funder')

        # Try to find user by email, or fall back to any user with the right role
        user = User.query.filter_by(email=email).first()
        if user:
            login_user(user, remember=True)
            return _redirect_by_role(user)

        # No matching email — just grab a default user for the selected role
        user = User.query.filter_by(role=role).first()
        if user:
            login_user(user, remember=True)
            return _redirect_by_role(user)

        flash('No accounts exist yet. Please register first.', 'danger')

    return render_template('auth/login.html')


# ── Registration ──────────────────────────────────────────────────
@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return _redirect_by_role(current_user)

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        display_name = request.form.get('display_name', '').strip()
        role = request.form.get('role', 'funder')
        cbo_name = request.form.get('cbo_name', '').strip()

        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return render_template('auth/register.html')

        user = User(email=email, role=role, display_name=display_name)
        user.set_password(password)

        # If CBO, create the organisation record
        if role == 'cbo' and cbo_name:
            cbo = CBO(name=cbo_name, slug=_slugify(cbo_name))
            db.session.add(cbo)
            db.session.flush()           # get cbo.id
            user.cbo_id = cbo.id

        db.session.add(user)
        db.session.commit()
        login_user(user, remember=True)
        flash('Account created!', 'success')
        return _redirect_by_role(user)

    return render_template('auth/register.html')


# ── Logout ────────────────────────────────────────────────────────
@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


# ── Role-based redirect ──────────────────────────────────────────
def _redirect_by_role(user: User):
    if user.role == 'funder':
        return redirect(url_for('main.marketplace'))
    return redirect(url_for('main.cbo_dashboard'))

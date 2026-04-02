import uuid
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def generate_uuid():
    return str(uuid.uuid4())


class WiFiPlan(db.Model):
    __tablename__ = 'wifi_plans'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    slug = db.Column(db.String(30), unique=True, nullable=False)
    badge = db.Column(db.String(20), nullable=False)
    badge_class = db.Column(db.String(20), default='')
    price = db.Column(db.Integer, nullable=False)  # KES
    price_label = db.Column(db.String(20), nullable=False)
    duration_seconds = db.Column(db.Integer, nullable=False)
    duration_label = db.Column(db.String(30), nullable=False)
    data_mb = db.Column(db.Integer, nullable=False)
    speed_down_kbps = db.Column(db.Integer, nullable=False)
    speed_up_kbps = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(100), default='')
    is_free = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)

    sessions = db.relationship('GuestSession', backref='plan', lazy=True)


class GuestSession(db.Model):
    __tablename__ = 'guest_sessions'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    mac_address = db.Column(db.String(17), nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    phone = db.Column(db.String(15), nullable=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('wifi_plans.id'), nullable=False)
    venue = db.Column(db.String(100), default='')
    status = db.Column(db.String(20), default='pending')  # pending, active, expired, terminated
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    activated_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    data_used_bytes = db.Column(db.BigInteger, default=0)
    nas_ip = db.Column(db.String(45), nullable=True)
    acct_session_id = db.Column(db.String(64), nullable=True)

    payment = db.relationship('Payment', backref='session', uselist=False, lazy=True)


class Payment(db.Model):
    __tablename__ = 'payments'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(36), db.ForeignKey('guest_sessions.id'), nullable=False)
    method = db.Column(db.String(20), nullable=False)  # mpesa, airtel, card, voucher, etc.
    amount = db.Column(db.Integer, nullable=False)
    phone = db.Column(db.String(15), nullable=True)
    account_ref = db.Column(db.String(50), nullable=True)
    transaction_id = db.Column(db.String(64), nullable=True)
    merchant_request_id = db.Column(db.String(64), nullable=True)
    checkout_request_id = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, completed, failed
    status_message = db.Column(db.String(200), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)


class Voucher(db.Model):
    __tablename__ = 'vouchers'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey('wifi_plans.id'), nullable=False)
    redeemed = db.Column(db.Boolean, default=False)
    redeemed_by = db.Column(db.String(15), nullable=True)
    redeemed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    plan = db.relationship('WiFiPlan', lazy=True)


class OTPRequest(db.Model):
    __tablename__ = 'otp_requests'

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(15), nullable=False)
    code = db.Column(db.String(6), nullable=False)
    verified = db.Column(db.Boolean, default=False)
    attempts = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)

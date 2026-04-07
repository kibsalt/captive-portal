import uuid
import re
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def generate_uuid():
    return str(uuid.uuid4())


def normalize_mac(mac):
    """Normalize MAC address to lowercase colon-separated format (aa:bb:cc:dd:ee:ff)."""
    if not mac:
        return None
    # Strip all separators, lowercase
    raw = re.sub(r'[^0-9a-fA-F]', '', mac).lower()
    if len(raw) != 12:
        return None
    return ':'.join(raw[i:i+2] for i in range(0, 12, 2))


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


class MacSession(db.Model):
    """Represents the accumulated WiFi access for a specific MAC address.

    This is the RADIUS-facing record. username=MAC, password=MAC.
    Multiple payments (M-Pesa, vouchers) stack onto the same MacSession,
    extending expires_at and adding data quota.

    Flow:
    1. Device connects → BRAS sends Access-Request(username=MAC, password=MAC)
    2. Portal checks MacSession: if active & not expired → Accept with QoS
    3. If no session or expired → Accept into walled garden (portal only)
    4. User pays → MacSession created/extended → CoA sent to BRAS
    """
    __tablename__ = 'mac_sessions'

    id = db.Column(db.Integer, primary_key=True)
    mac_address = db.Column(db.String(17), unique=True, nullable=False, index=True)
    status = db.Column(db.String(20), default='walled')  # walled, active, expired
    # Accumulated quotas (stacked from multiple payments)
    total_seconds = db.Column(db.Integer, default=0)
    total_data_bytes = db.Column(db.BigInteger, default=0)
    data_used_bytes = db.Column(db.BigInteger, default=0)
    # Current QoS (highest tier from active payments)
    speed_down_kbps = db.Column(db.Integer, default=2048)
    speed_up_kbps = db.Column(db.Integer, default=1024)
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    first_activated_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    last_coa_at = db.Column(db.DateTime, nullable=True)
    # RADIUS session tracking
    acct_session_id = db.Column(db.String(64), nullable=True)
    nas_ip = db.Column(db.String(45), nullable=True)
    # Linked phone (most recent)
    phone = db.Column(db.String(15), nullable=True)
    venue = db.Column(db.String(100), default='')

    # All payments stacked onto this MAC
    credits = db.relationship('MacCredit', backref='mac_session', lazy=True,
                              order_by='MacCredit.created_at.desc()')

    @property
    def is_active(self):
        if self.status != 'active':
            return False
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False
        if self.total_data_bytes > 0 and self.data_used_bytes >= self.total_data_bytes:
            return False
        return True

    @property
    def remaining_seconds(self):
        if not self.expires_at:
            return 0
        remaining = (self.expires_at - datetime.utcnow()).total_seconds()
        return max(0, int(remaining))

    @property
    def remaining_data_bytes(self):
        if self.total_data_bytes <= 0:
            return 0
        return max(0, self.total_data_bytes - self.data_used_bytes)

    @property
    def radius_class(self):
        """Determine the RADIUS class based on highest active speed tier."""
        if self.speed_down_kbps >= 51200:
            return 'GUEST-PREMIUM'
        elif self.speed_down_kbps >= 20480:
            return 'GUEST-STANDARD'
        elif self.speed_down_kbps >= 10240:
            return 'GUEST-BASIC'
        else:
            return 'GUEST-FREE'


class MacCredit(db.Model):
    """Individual payment/voucher credit stacked onto a MacSession.

    Each M-Pesa payment, voucher redemption, or free tier activation creates
    a MacCredit that extends the parent MacSession's quotas.
    """
    __tablename__ = 'mac_credits'

    id = db.Column(db.Integer, primary_key=True)
    mac_session_id = db.Column(db.Integer, db.ForeignKey('mac_sessions.id'), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey('wifi_plans.id'), nullable=False)
    # What activated this credit
    credit_type = db.Column(db.String(20), nullable=False)  # mpesa, voucher, free, airtel, etc.
    transaction_code = db.Column(db.String(64), nullable=True)  # M-Pesa code, voucher code
    phone = db.Column(db.String(15), nullable=True)
    amount_paid = db.Column(db.Integer, default=0)
    # What this credit adds
    seconds_added = db.Column(db.Integer, default=0)
    data_bytes_added = db.Column(db.BigInteger, default=0)
    speed_down_kbps = db.Column(db.Integer, default=0)
    speed_up_kbps = db.Column(db.Integer, default=0)
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    plan = db.relationship('WiFiPlan', lazy=True)


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

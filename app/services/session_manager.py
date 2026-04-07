"""Guest WiFi session lifecycle management.

Handles:
- MAC-based session creation and credit stacking
- RADIUS CoA for dynamic session control
- Session expiry checks and RADIUS Disconnect
- Legacy GuestSession activation (backwards compatible)
"""

import logging
from datetime import datetime, timedelta

from app.models import db, GuestSession, MacSession, MacCredit, WiFiPlan, normalize_mac

logger = logging.getLogger(__name__)


# ─── MAC-based session management (primary) ─────────────────────────────

def activate_mac_session(mac_address, plan_id, credit_type, transaction_code=None,
                         phone=None, amount_paid=0, venue=''):
    """Create or extend a MacSession by stacking a new credit.

    This is the core function called after any successful payment:
    - M-Pesa STK push confirmed
    - M-Pesa code entered manually
    - Voucher redeemed
    - Free tier OTP verified

    If the MAC already has an active session, the new plan's time and data
    are ADDED to the existing session (stacking). Speed is upgraded to the
    highest tier across all active credits.

    Args:
        mac_address: Device MAC (any format, will be normalized)
        plan_id: WiFi plan to activate
        credit_type: 'mpesa', 'voucher', 'free', 'airtel', etc.
        transaction_code: M-Pesa code or voucher code
        phone: Phone number used
        amount_paid: Amount in KES
        venue: Venue name

    Returns:
        dict with session info, or None on failure
    """
    mac = normalize_mac(mac_address)
    if not mac:
        logger.error(f'Invalid MAC address: {mac_address}')
        return None

    plan = WiFiPlan.query.get(plan_id)
    if not plan:
        logger.error(f'Plan not found: {plan_id}')
        return None

    # Check for duplicate transaction code
    if transaction_code:
        existing_credit = MacCredit.query.filter_by(transaction_code=transaction_code).first()
        if existing_credit:
            logger.warning(f'Duplicate transaction code: {transaction_code}')
            return None

    # Find or create MacSession for this MAC
    mac_session = MacSession.query.filter_by(mac_address=mac).first()
    now = datetime.utcnow()

    if not mac_session:
        mac_session = MacSession(
            mac_address=mac,
            status='active',
            first_activated_at=now,
            phone=phone,
            venue=venue,
        )
        db.session.add(mac_session)
        db.session.flush()

    # Create the credit record
    credit = MacCredit(
        mac_session_id=mac_session.id,
        plan_id=plan.id,
        credit_type=credit_type,
        transaction_code=transaction_code,
        phone=phone,
        amount_paid=amount_paid,
        seconds_added=plan.duration_seconds,
        data_bytes_added=plan.data_mb * 1024 * 1024,
        speed_down_kbps=plan.speed_down_kbps,
        speed_up_kbps=plan.speed_up_kbps,
    )
    db.session.add(credit)

    # Stack the credit onto the session
    if mac_session.status == 'active' and mac_session.expires_at and mac_session.expires_at > now:
        # Extend existing active session
        mac_session.expires_at += timedelta(seconds=plan.duration_seconds)
        mac_session.total_seconds += plan.duration_seconds
        mac_session.total_data_bytes += plan.data_mb * 1024 * 1024
    else:
        # New session or expired — start fresh from now
        mac_session.status = 'active'
        mac_session.expires_at = now + timedelta(seconds=plan.duration_seconds)
        mac_session.total_seconds = plan.duration_seconds
        mac_session.total_data_bytes = plan.data_mb * 1024 * 1024
        mac_session.data_used_bytes = 0
        if not mac_session.first_activated_at:
            mac_session.first_activated_at = now

    # Upgrade speed to the highest across all credits
    mac_session.speed_down_kbps = max(mac_session.speed_down_kbps, plan.speed_down_kbps)
    mac_session.speed_up_kbps = max(mac_session.speed_up_kbps, plan.speed_up_kbps)

    # Update phone and venue
    if phone:
        mac_session.phone = phone
    if venue:
        mac_session.venue = venue

    mac_session.acct_session_id = f'FAIBA-{mac.replace(":", "").upper()}'

    db.session.commit()

    # Send RADIUS CoA to BRAS to update QoS / unlock internet
    _send_radius_coa_for_mac(mac_session)

    logger.info(
        f'MAC session {"extended" if credit.seconds_added else "activated"}: {mac} | '
        f'Plan: {plan.name} | Credit: {credit_type} | Code: {transaction_code} | '
        f'Expires: {mac_session.expires_at} | '
        f'Total: {mac_session.total_seconds}s / {mac_session.total_data_bytes // (1024*1024)}MB'
    )

    return {
        'mac': mac,
        'status': 'active',
        'plan_name': plan.name,
        'expires_at': mac_session.expires_at.isoformat(),
        'remaining_seconds': mac_session.remaining_seconds,
        'remaining_data_mb': mac_session.remaining_data_bytes // (1024 * 1024),
        'speed_down_kbps': mac_session.speed_down_kbps,
        'credits_count': len(mac_session.credits),
    }


def activate_mac_session_from_external(mac_address, voucher_code, phone=None,
                                       amount=0, upspeed_kbps=0, downspeed_kbps=0,
                                       downlimit_bytes=0, session_end=0, venue=''):
    """Create or extend a MacSession using QoS data from the Lexabensa API.

    Unlike activate_mac_session which looks up a local WiFiPlan, this uses the
    raw QoS values returned by Lexabensa (upspeed, downspeed, downlimit, amount).

    Lexabensa API response format:
        voucher,upspeed,downspeed,downlimit,amount,session_end,start_date

    The session_end field determines duration:
    - session_end == 0: unused voucher → use amount to determine duration
    - session_end > 0: already has an end time → calculate remaining

    Args:
        mac_address: Device MAC
        voucher_code: M-Pesa receipt code (from Lexabensa 'voucher' field)
        phone: Phone number
        amount: Amount paid in KES
        upspeed_kbps: Upload speed from Lexabensa
        downspeed_kbps: Download speed from Lexabensa
        downlimit_bytes: Data limit from Lexabensa (0 = unlimited)
        session_end: Unix timestamp for session end (0 = unused)
        venue: Venue name
    """
    mac = normalize_mac(mac_address)
    if not mac:
        logger.error(f'Invalid MAC address: {mac_address}')
        return None

    # Check for duplicate
    existing_credit = MacCredit.query.filter_by(transaction_code=voucher_code).first()
    if existing_credit:
        logger.warning(f'Duplicate external voucher code: {voucher_code}')
        return None

    # Determine session duration from amount (price → duration mapping)
    # This maps to the seeded plans:  30→1h, 50→3h, 100→24h, 150→2d, 250→7d, 350→30d, 500→30d, 1000→30d
    duration_map = [
        (1000, 2592000),  # KES 1000 → 30 days
        (500, 2592000),   # KES 500 → 30 days
        (350, 2592000),   # KES 350 → 30 days
        (250, 604800),    # KES 250 → 7 days
        (150, 172800),    # KES 150 → 2 days
        (100, 86400),     # KES 100 → 24 hours
        (50, 10800),      # KES 50 → 3 hours
        (30, 3600),       # KES 30 → 1 hour
        (10, 3600),       # KES 10 → 1 hour
        (5, 1800),        # KES 5 → 30 min
    ]

    if session_end > 0:
        # External DB already has an end time — calculate remaining seconds
        import time
        remaining = max(0, session_end - int(time.time()))
        duration_seconds = remaining if remaining > 0 else 3600
    else:
        # Unused voucher — determine duration from amount
        duration_seconds = 3600  # default 1 hour
        for threshold, seconds in duration_map:
            if amount >= threshold:
                duration_seconds = seconds
                break

    # Find or create MacSession
    mac_session = MacSession.query.filter_by(mac_address=mac).first()
    now = datetime.utcnow()

    if not mac_session:
        mac_session = MacSession(
            mac_address=mac,
            status='active',
            first_activated_at=now,
            phone=phone,
            venue=venue,
        )
        db.session.add(mac_session)
        db.session.flush()

    # Create credit record (plan_id=0 since this comes from external DB)
    # Find the closest matching plan or use plan_id=1 as fallback
    closest_plan_id = _find_closest_plan(amount)

    credit = MacCredit(
        mac_session_id=mac_session.id,
        plan_id=closest_plan_id,
        credit_type='mpesa',
        transaction_code=voucher_code,
        phone=phone,
        amount_paid=amount,
        seconds_added=duration_seconds,
        data_bytes_added=downlimit_bytes,
        speed_down_kbps=downspeed_kbps,
        speed_up_kbps=upspeed_kbps,
    )
    db.session.add(credit)

    # Stack onto session
    if mac_session.status == 'active' and mac_session.expires_at and mac_session.expires_at > now:
        mac_session.expires_at += timedelta(seconds=duration_seconds)
        mac_session.total_seconds += duration_seconds
        if downlimit_bytes > 0:
            mac_session.total_data_bytes += downlimit_bytes
    else:
        mac_session.status = 'active'
        mac_session.expires_at = now + timedelta(seconds=duration_seconds)
        mac_session.total_seconds = duration_seconds
        mac_session.total_data_bytes = downlimit_bytes
        mac_session.data_used_bytes = 0
        if not mac_session.first_activated_at:
            mac_session.first_activated_at = now

    # Use the external DB's speed values
    if downspeed_kbps > 0:
        mac_session.speed_down_kbps = max(mac_session.speed_down_kbps, downspeed_kbps)
    if upspeed_kbps > 0:
        mac_session.speed_up_kbps = max(mac_session.speed_up_kbps, upspeed_kbps)

    if phone:
        mac_session.phone = phone
    if venue:
        mac_session.venue = venue

    mac_session.acct_session_id = f'FAIBA-{mac.replace(":", "").upper()}'

    db.session.commit()

    # Send RADIUS CoA
    _send_radius_coa_for_mac(mac_session)

    logger.info(
        f'MAC session from Lexabensa: {mac} | Voucher: {voucher_code} | '
        f'KES {amount} | {duration_seconds}s | '
        f'{downspeed_kbps}kbps down / {upspeed_kbps}kbps up | '
        f'Limit: {downlimit_bytes} bytes | Expires: {mac_session.expires_at}'
    )

    return {
        'mac': mac,
        'status': 'active',
        'voucher_code': voucher_code,
        'expires_at': mac_session.expires_at.isoformat(),
        'remaining_seconds': mac_session.remaining_seconds,
        'remaining_data_mb': mac_session.remaining_data_bytes // (1024 * 1024) if downlimit_bytes > 0 else None,
        'speed_down_kbps': mac_session.speed_down_kbps,
        'credits_count': len(mac_session.credits),
    }


def _find_closest_plan(amount):
    """Find the WiFiPlan ID closest to the given amount. Fallback to 1."""
    plan = WiFiPlan.query.filter(
        WiFiPlan.price <= amount,
        WiFiPlan.is_free == False,
        WiFiPlan.active == True,
    ).order_by(WiFiPlan.price.desc()).first()
    return plan.id if plan else 1


# ─── Legacy GuestSession activation (backwards compatible) ──────────────

def activate_session(session_id):
    """Activate a guest WiFi session after payment/OTP verification.

    This is the legacy flow. If the GuestSession has a mac_address,
    it delegates to activate_mac_session for stacking support.
    """
    session = GuestSession.query.get(session_id)
    if not session:
        logger.error(f'Session not found: {session_id}')
        return False

    plan = session.plan
    now = datetime.utcnow()

    # If MAC is available, use the new MAC-based stacking system
    if session.mac_address:
        mac = normalize_mac(session.mac_address)
        if mac:
            tx_code = None
            credit_type = 'unknown'
            if session.payment:
                tx_code = session.payment.transaction_id
                credit_type = session.payment.method
            result = activate_mac_session(
                mac_address=mac,
                plan_id=plan.id,
                credit_type=credit_type,
                transaction_code=tx_code,
                phone=session.phone,
                amount_paid=session.payment.amount if session.payment else 0,
                venue=session.venue,
            )
            if result:
                session.status = 'active'
                session.activated_at = now
                session.expires_at = now + timedelta(seconds=plan.duration_seconds)
                session.acct_session_id = f'FAIBA-{session.id[:12].upper()}'
                db.session.commit()
                return True

    # Fallback: legacy activation without MAC stacking
    session.status = 'active'
    session.activated_at = now
    session.expires_at = now + timedelta(seconds=plan.duration_seconds)
    session.acct_session_id = f'FAIBA-{session.id[:12].upper()}'
    db.session.commit()

    _send_radius_coa(session)

    logger.info(
        f'Session activated (legacy): {session.id} | Plan: {plan.name} | '
        f'Phone: {session.phone} | Expires: {session.expires_at}'
    )
    return True


def terminate_session(session_id):
    """Terminate an active session."""
    session = GuestSession.query.get(session_id)
    if not session:
        return False

    session.status = 'terminated'
    db.session.commit()

    _send_radius_disconnect(session)

    logger.info(f'Session terminated: {session.id}')
    return True


def terminate_mac_session(mac_address):
    """Terminate a MAC session and disconnect from BRAS."""
    mac = normalize_mac(mac_address)
    if not mac:
        return False

    mac_session = MacSession.query.filter_by(mac_address=mac).first()
    if not mac_session:
        return False

    mac_session.status = 'expired'
    db.session.commit()

    _send_disconnect_for_mac(mac_session)

    logger.info(f'MAC session terminated: {mac}')
    return True


# ─── Background job: check expired sessions ─────────────────────────────

def check_expired_sessions(app):
    """Background job: check for expired sessions and disconnect them."""
    with app.app_context():
        now = datetime.utcnow()

        # Check legacy GuestSessions
        expired_sessions = GuestSession.query.filter(
            GuestSession.status == 'active',
            GuestSession.expires_at <= now
        ).all()

        for session in expired_sessions:
            session.status = 'expired'
            db.session.commit()
            _send_radius_disconnect(session)
            logger.info(f'Session expired: {session.id} | Phone: {session.phone}')

        # Check MacSessions
        expired_macs = MacSession.query.filter(
            MacSession.status == 'active',
            MacSession.expires_at <= now
        ).all()

        for mac_session in expired_macs:
            mac_session.status = 'expired'
            db.session.commit()
            _send_disconnect_for_mac(mac_session)
            logger.info(f'MAC session expired: {mac_session.mac_address}')

        total_expired = len(expired_sessions) + len(expired_macs)
        if total_expired:
            logger.info(f'Expired {total_expired} session(s)')


# ─── RADIUS CoA/Disconnect helpers ──────────────────────────────────────

def _send_radius_coa_for_mac(mac_session):
    """Send RADIUS CoA for a MacSession to unlock/update internet access."""
    from flask import current_app

    if not mac_session.acct_session_id:
        return

    try:
        from app.services.radius_client import send_coa

        result = send_coa(
            server=current_app.config['RADIUS_SERVER'],
            port=current_app.config['RADIUS_COA_PORT'],
            secret=current_app.config['RADIUS_SECRET'],
            session_id=mac_session.acct_session_id,
            nas_ip=mac_session.nas_ip or current_app.config['RADIUS_SERVER'],
            session_timeout=mac_session.remaining_seconds,
            speed_down_kbps=mac_session.speed_down_kbps,
            speed_up_kbps=mac_session.speed_up_kbps,
            data_limit_bytes=mac_session.total_data_bytes,
            session_class=mac_session.radius_class,
        )

        if result:
            mac_session.last_coa_at = datetime.utcnow()
            db.session.commit()
            logger.info(f'RADIUS CoA sent for MAC {mac_session.mac_address}')
        else:
            logger.warning(f'RADIUS CoA failed for MAC {mac_session.mac_address}')

    except Exception as e:
        logger.error(f'RADIUS CoA error for MAC {mac_session.mac_address}: {e}')


def _send_disconnect_for_mac(mac_session):
    """Send RADIUS Disconnect-Message for a MacSession."""
    from flask import current_app

    if not mac_session.acct_session_id:
        return

    try:
        from app.services.radius_client import send_disconnect

        result = send_disconnect(
            server=current_app.config['RADIUS_SERVER'],
            port=current_app.config['RADIUS_COA_PORT'],
            secret=current_app.config['RADIUS_SECRET'],
            session_id=mac_session.acct_session_id,
            nas_ip=mac_session.nas_ip or current_app.config['RADIUS_SERVER'],
        )

        if result:
            logger.info(f'RADIUS DM sent for MAC {mac_session.mac_address}')
        else:
            logger.warning(f'RADIUS DM failed for MAC {mac_session.mac_address}')

    except Exception as e:
        logger.error(f'RADIUS DM error for MAC {mac_session.mac_address}: {e}')


def _send_radius_coa(session):
    """Send RADIUS CoA for a legacy GuestSession."""
    from flask import current_app

    try:
        from app.services.radius_client import send_coa

        plan = session.plan
        class_map = {
            'hourly': 'GUEST-HOURLY',
            '3hour': 'GUEST-HOURLY',
            'daily': 'GUEST-DAY',
            'weekend': 'GUEST-DAY',
            'weekly': 'GUEST-WEEK',
            'student': 'GUEST-MONTH',
            'monthly': 'GUEST-MONTH',
            'unlimited': 'GUEST-PREMIUM',
            'free': 'GUEST-FREE',
        }
        session_class = class_map.get(plan.slug, 'GUEST-DEFAULT')

        result = send_coa(
            server=current_app.config['RADIUS_SERVER'],
            port=current_app.config['RADIUS_COA_PORT'],
            secret=current_app.config['RADIUS_SECRET'],
            session_id=session.acct_session_id,
            nas_ip=current_app.config['RADIUS_SERVER'],
            session_timeout=plan.duration_seconds,
            speed_down_kbps=plan.speed_down_kbps,
            speed_up_kbps=plan.speed_up_kbps,
            data_limit_bytes=plan.data_mb * 1024 * 1024,
            session_class=session_class,
        )

        if result:
            logger.info(f'RADIUS CoA sent for session {session.id}')
        else:
            logger.warning(f'RADIUS CoA failed for session {session.id} (session still activated)')

    except Exception as e:
        logger.error(f'RADIUS CoA error for session {session.id}: {e}')


def _send_radius_disconnect(session):
    """Send RADIUS Disconnect-Message for a legacy GuestSession."""
    from flask import current_app

    if not session.acct_session_id:
        return

    try:
        from app.services.radius_client import send_disconnect

        result = send_disconnect(
            server=current_app.config['RADIUS_SERVER'],
            port=current_app.config['RADIUS_COA_PORT'],
            secret=current_app.config['RADIUS_SECRET'],
            session_id=session.acct_session_id,
            nas_ip=current_app.config['RADIUS_SERVER'],
        )

        if result:
            logger.info(f'RADIUS DM sent for session {session.id}')
        else:
            logger.warning(f'RADIUS DM failed for session {session.id}')

    except Exception as e:
        logger.error(f'RADIUS DM error for session {session.id}: {e}')

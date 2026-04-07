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

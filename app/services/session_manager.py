"""Guest WiFi session lifecycle management.

Handles session creation, activation (RADIUS CoA), expiry checks,
and termination (RADIUS Disconnect-Message).
"""

import logging
from datetime import datetime, timedelta

from app.models import db, GuestSession

logger = logging.getLogger(__name__)


def activate_session(session_id):
    """Activate a guest WiFi session after payment/OTP verification.

    1. Update session status to 'active'
    2. Set activation and expiry timestamps
    3. Send RADIUS CoA to BRAS to unlock internet access
    """
    session = GuestSession.query.get(session_id)
    if not session:
        logger.error(f'Session not found: {session_id}')
        return False

    plan = session.plan
    now = datetime.utcnow()

    session.status = 'active'
    session.activated_at = now
    session.expires_at = now + timedelta(seconds=plan.duration_seconds)
    session.acct_session_id = f'FAIBA-{session.id[:12].upper()}'
    db.session.commit()

    # Send RADIUS CoA to unlock internet
    _send_radius_coa(session)

    logger.info(
        f'Session activated: {session.id} | Plan: {plan.name} | '
        f'Phone: {session.phone} | Expires: {session.expires_at}'
    )
    return True


def terminate_session(session_id):
    """Terminate an active session.

    1. Update session status to 'terminated'
    2. Send RADIUS Disconnect-Message to BRAS
    """
    session = GuestSession.query.get(session_id)
    if not session:
        return False

    session.status = 'terminated'
    db.session.commit()

    _send_radius_disconnect(session)

    logger.info(f'Session terminated: {session.id}')
    return True


def check_expired_sessions(app):
    """Background job: check for expired sessions and disconnect them."""
    with app.app_context():
        now = datetime.utcnow()
        expired_sessions = GuestSession.query.filter(
            GuestSession.status == 'active',
            GuestSession.expires_at <= now
        ).all()

        for session in expired_sessions:
            session.status = 'expired'
            db.session.commit()

            _send_radius_disconnect(session)

            logger.info(f'Session expired: {session.id} | Phone: {session.phone}')

        if expired_sessions:
            logger.info(f'Expired {len(expired_sessions)} session(s)')


def _send_radius_coa(session):
    """Send RADIUS Change of Authorization to activate internet access."""
    from flask import current_app

    try:
        from app.services.radius_client import send_coa

        plan = session.plan
        # Map plan to RADIUS class
        class_map = {
            'hourly': 'GUEST-HOURLY',
            'daily': 'GUEST-DAY',
            'weekly': 'GUEST-WEEK',
            'monthly': 'GUEST-MONTH',
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
    """Send RADIUS Disconnect-Message to terminate internet access."""
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

import logging
from datetime import datetime

from flask import Blueprint, request, jsonify

from app.models import db, GuestSession

logger = logging.getLogger(__name__)

session_bp = Blueprint('session', __name__)


@session_bp.route('/status/<session_id>')
def session_status(session_id):
    """Get current session status."""
    session = GuestSession.query.get(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    plan = session.plan
    remaining_seconds = 0
    if session.status == 'active' and session.expires_at:
        remaining_seconds = max(0, int((session.expires_at - datetime.utcnow()).total_seconds()))
        if remaining_seconds == 0:
            session.status = 'expired'
            db.session.commit()

    data_remaining_mb = max(0, plan.data_mb - (session.data_used_bytes / (1024 * 1024)))

    return jsonify({
        'session_id': session.id,
        'status': session.status,
        'plan': plan.name,
        'plan_slug': plan.slug,
        'venue': session.venue,
        'activated_at': session.activated_at.isoformat() if session.activated_at else None,
        'expires_at': session.expires_at.isoformat() if session.expires_at else None,
        'remaining_seconds': remaining_seconds,
        'data_used_mb': round(session.data_used_bytes / (1024 * 1024), 2),
        'data_remaining_mb': round(data_remaining_mb, 2),
        'data_total_mb': plan.data_mb,
        'speed_down_kbps': plan.speed_down_kbps,
        'speed_up_kbps': plan.speed_up_kbps,
    })


@session_bp.route('/active')
def active_sessions():
    """List active sessions (admin endpoint)."""
    auth = request.authorization
    if not auth:
        return jsonify({'error': 'Authentication required'}), 401

    from app.config import Config
    if auth.username != Config.ADMIN_USERNAME or auth.password != Config.ADMIN_PASSWORD:
        return jsonify({'error': 'Invalid credentials'}), 403

    sessions = GuestSession.query.filter_by(status='active').all()
    return jsonify([{
        'id': s.id,
        'phone': s.phone,
        'plan': s.plan.name,
        'venue': s.venue,
        'ip_address': s.ip_address,
        'mac_address': s.mac_address,
        'activated_at': s.activated_at.isoformat() if s.activated_at else None,
        'expires_at': s.expires_at.isoformat() if s.expires_at else None,
    } for s in sessions])


@session_bp.route('/terminate/<session_id>', methods=['POST'])
def terminate_session(session_id):
    """Manually terminate a session (admin endpoint)."""
    auth = request.authorization
    if not auth:
        return jsonify({'error': 'Authentication required'}), 401

    from app.config import Config
    if auth.username != Config.ADMIN_USERNAME or auth.password != Config.ADMIN_PASSWORD:
        return jsonify({'error': 'Invalid credentials'}), 403

    session = GuestSession.query.get(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    if session.status != 'active':
        return jsonify({'error': f'Session is not active (status: {session.status})'}), 400

    from app.services.session_manager import terminate_session as do_terminate
    do_terminate(session_id)

    return jsonify({'status': 'terminated', 'session_id': session_id})

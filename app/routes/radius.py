"""RADIUS REST API endpoints for FreeRADIUS rlm_rest integration.

FreeRADIUS calls these endpoints for:
- Authorization: Check if MAC has active session → return Accept with QoS or walled garden
- Authentication: Verify MAC=MAC credentials (always accept, authorization controls access)
- Accounting: Track session start/stop/interim-update for data usage

RADIUS flow:
1. Device connects → BRAS sends Access-Request(User-Name=MAC, User-Password=MAC)
2. FreeRADIUS rlm_rest calls POST /api/radius/auth
3. Portal checks MacSession:
   - Active + not expired → Accept with full internet QoS attributes
   - No session or expired → Accept into walled garden (captive portal only)
4. User pays (STK push / M-Pesa code / voucher) → Portal creates/extends MacSession
5. Portal sends CoA to BRAS → Internet unlocked with new QoS
6. Next payment → extends same MacSession (stacks time + data)
"""

import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app

from app.models import db, MacSession, MacCredit, normalize_mac

logger = logging.getLogger(__name__)

radius_bp = Blueprint('radius', __name__)


@radius_bp.route('/auth', methods=['POST'])
def radius_auth():
    """Handle FreeRADIUS authorization + authentication via rlm_rest.

    FreeRADIUS sends:
        User-Name: MAC address (various formats: AA-BB-CC-DD-EE-FF, aabb.ccdd.eeff, etc.)
        User-Password: MAC address (same as username — transparent MAC auth)
        NAS-IP-Address: BRAS/NAS IP
        Calling-Station-Id: Client MAC (may also be in this field)
        Called-Station-Id: AP SSID/MAC
        NAS-Identifier: NAS name
        Framed-IP-Address: Client IP (if known)

    Returns JSON that rlm_rest maps to RADIUS attributes:
        - For active sessions: Access-Accept with full QoS
        - For no/expired sessions: Access-Accept with walled garden (portal-only)
    """
    data = request.get_json(silent=True) or {}

    # Extract MAC from User-Name or Calling-Station-Id
    username = data.get('User-Name', '')
    calling_station = data.get('Calling-Station-Id', '')
    password = data.get('User-Password', '')
    nas_ip = data.get('NAS-IP-Address', '')
    framed_ip = data.get('Framed-IP-Address', '')

    mac = normalize_mac(username) or normalize_mac(calling_station)
    if not mac:
        logger.warning(f'RADIUS auth: could not extract MAC from User-Name={username} '
                       f'Calling-Station-Id={calling_station}')
        return jsonify({
            'control:Auth-Type': 'Accept',
            'reply:Filter-Id': 'WALLED-GARDEN',
            'reply:Session-Timeout': 300,
            'reply:Reply-Message': 'Unknown device. Connect to portal.',
        })

    # Verify password = MAC (transparent MAC auth)
    expected_password = mac
    given_password = normalize_mac(password)
    if given_password and given_password != expected_password:
        logger.info(f'RADIUS auth: password mismatch for {mac}')
        return jsonify({
            'control:Auth-Type': 'Reject',
            'reply:Reply-Message': 'Authentication failed',
        }), 401

    # Look up MAC session
    mac_session = MacSession.query.filter_by(mac_address=mac).first()

    if not mac_session:
        # First time seeing this MAC — create walled garden entry
        mac_session = MacSession(
            mac_address=mac,
            status='walled',
            nas_ip=nas_ip,
        )
        db.session.add(mac_session)
        db.session.commit()
        logger.info(f'RADIUS auth: new MAC {mac} → walled garden')

        return jsonify({
            'control:Auth-Type': 'Accept',
            'reply:Filter-Id': 'WALLED-GARDEN',
            'reply:Session-Timeout': 300,
            'reply:Idle-Timeout': 120,
            'reply:Class': 'GUEST-WALLED',
            'reply:Reply-Message': 'Please open browser to connect.',
            'reply:WISPr-Redirection-URL': f'http://{current_app.config["PORTAL_HOST"]}:{current_app.config["PORTAL_PORT"]}/?mac={mac}',
        })

    # Update NAS info
    mac_session.nas_ip = nas_ip

    # Check if session is active
    if mac_session.is_active:
        # Active paid session — full internet access with QoS
        remaining = mac_session.remaining_seconds
        db.session.commit()

        logger.info(f'RADIUS auth: {mac} → active, {remaining}s remaining, '
                    f'{mac_session.speed_down_kbps}kbps down')

        return jsonify({
            'control:Auth-Type': 'Accept',
            'reply:Filter-Id': 'INTERNET-ACCESS',
            'reply:Session-Timeout': remaining,
            'reply:Idle-Timeout': 600,
            'reply:Acct-Interim-Interval': 60,
            'reply:Class': mac_session.radius_class,
            'reply:Reply-Message': f'Welcome. Session expires in {remaining // 60} minutes.',
            'reply:WISPr-Bandwidth-Max-Down': mac_session.speed_down_kbps * 1000,
            'reply:WISPr-Bandwidth-Max-Up': mac_session.speed_up_kbps * 1000,
        })

    # Expired or walled — send to captive portal
    if mac_session.status == 'active' and not mac_session.is_active:
        mac_session.status = 'expired'

    db.session.commit()

    logger.info(f'RADIUS auth: {mac} → walled garden (status={mac_session.status})')

    return jsonify({
        'control:Auth-Type': 'Accept',
        'reply:Filter-Id': 'WALLED-GARDEN',
        'reply:Session-Timeout': 300,
        'reply:Idle-Timeout': 120,
        'reply:Class': 'GUEST-WALLED',
        'reply:Reply-Message': 'Session expired. Please purchase a new plan.',
        'reply:WISPr-Redirection-URL': f'http://{current_app.config["PORTAL_HOST"]}:{current_app.config["PORTAL_PORT"]}/?mac={mac}',
    })


@radius_bp.route('/acct', methods=['POST'])
def radius_accounting():
    """Handle RADIUS accounting updates from FreeRADIUS rlm_rest.

    FreeRADIUS sends accounting data:
        Acct-Status-Type: Start, Interim-Update, Stop
        User-Name: MAC address
        Acct-Session-Id: RADIUS session ID
        Acct-Input-Octets: Bytes uploaded by client
        Acct-Output-Octets: Bytes downloaded by client
        Acct-Session-Time: Seconds since session start
        Framed-IP-Address: Client IP
    """
    data = request.get_json(silent=True) or {}

    acct_type = data.get('Acct-Status-Type', '')
    username = data.get('User-Name', '')
    acct_session_id = data.get('Acct-Session-Id', '')
    input_octets = int(data.get('Acct-Input-Octets', 0))
    output_octets = int(data.get('Acct-Output-Octets', 0))
    input_gigawords = int(data.get('Acct-Input-Gigawords', 0))
    output_gigawords = int(data.get('Acct-Output-Gigawords', 0))

    mac = normalize_mac(username)
    if not mac:
        return jsonify({'status': 'ok'})

    # Calculate total bytes (handle 32-bit counter wraparound via Gigawords)
    total_input = input_octets + (input_gigawords * 2**32)
    total_output = output_octets + (output_gigawords * 2**32)
    total_bytes = total_input + total_output

    mac_session = MacSession.query.filter_by(mac_address=mac).first()
    if not mac_session:
        logger.warning(f'RADIUS acct: no MacSession for {mac}')
        return jsonify({'status': 'ok'})

    if acct_type == 'Start':
        mac_session.acct_session_id = acct_session_id
        db.session.commit()
        logger.info(f'RADIUS acct Start: {mac} session_id={acct_session_id}')

    elif acct_type == 'Interim-Update':
        mac_session.data_used_bytes = total_bytes
        mac_session.acct_session_id = acct_session_id
        db.session.commit()

        # Check if data quota exceeded → disconnect
        if mac_session.total_data_bytes > 0 and total_bytes >= mac_session.total_data_bytes:
            logger.info(f'RADIUS acct: {mac} data quota exceeded '
                        f'({total_bytes}/{mac_session.total_data_bytes})')
            mac_session.status = 'expired'
            db.session.commit()
            # Send disconnect
            _send_disconnect_for_mac(mac_session)

    elif acct_type == 'Stop':
        mac_session.data_used_bytes = total_bytes
        if mac_session.status == 'active' and not mac_session.is_active:
            mac_session.status = 'expired'
        db.session.commit()
        logger.info(f'RADIUS acct Stop: {mac} total_bytes={total_bytes}')

    return jsonify({'status': 'ok'})


@radius_bp.route('/mac/<mac_address>', methods=['GET'])
def get_mac_session(mac_address):
    """Get current session status for a MAC address.
    Used by the frontend to check if the device already has an active session.
    """
    mac = normalize_mac(mac_address)
    if not mac:
        return jsonify({'error': 'Invalid MAC address'}), 400

    mac_session = MacSession.query.filter_by(mac_address=mac).first()
    if not mac_session:
        return jsonify({'status': 'none', 'mac': mac})

    return jsonify({
        'status': mac_session.status,
        'mac': mac,
        'is_active': mac_session.is_active,
        'remaining_seconds': mac_session.remaining_seconds,
        'remaining_data_mb': mac_session.remaining_data_bytes // (1024 * 1024) if mac_session.total_data_bytes > 0 else None,
        'speed_down_kbps': mac_session.speed_down_kbps,
        'speed_up_kbps': mac_session.speed_up_kbps,
        'expires_at': mac_session.expires_at.isoformat() if mac_session.expires_at else None,
        'credits_count': len(mac_session.credits),
    })


def _send_disconnect_for_mac(mac_session):
    """Send RADIUS Disconnect-Message when data quota is exceeded."""
    if not mac_session.acct_session_id or not mac_session.nas_ip:
        return
    try:
        from app.services.radius_client import send_disconnect
        send_disconnect(
            server=current_app.config['RADIUS_SERVER'],
            port=current_app.config['RADIUS_COA_PORT'],
            secret=current_app.config['RADIUS_SECRET'],
            session_id=mac_session.acct_session_id,
            nas_ip=mac_session.nas_ip,
        )
    except Exception as e:
        logger.error(f'RADIUS disconnect error for MAC {mac_session.mac_address}: {e}')

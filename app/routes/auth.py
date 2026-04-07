import random
import logging
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, current_app

from app.models import db, WiFiPlan, GuestSession, OTPRequest, Voucher, normalize_mac
from app.services.session_manager import activate_session, activate_mac_session

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/otp/send', methods=['POST'])
def send_otp():
    """Send SMS OTP for free tier access.

    Request JSON:
        phone: str - Kenyan phone number (07XX XXX XXX)
    """
    data = request.get_json()
    if not data or not data.get('phone'):
        return jsonify({'error': 'Phone number required'}), 400

    phone = data['phone'].strip().replace(' ', '')
    if not phone.startswith('07') and not phone.startswith('01') and not phone.startswith('+254'):
        return jsonify({'error': 'Invalid Kenyan phone number'}), 400

    # Rate limit: max 3 OTPs per phone per hour
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_count = OTPRequest.query.filter(
        OTPRequest.phone == phone,
        OTPRequest.created_at > one_hour_ago
    ).count()
    if recent_count >= 3:
        return jsonify({'error': 'Too many OTP requests. Try again later.'}), 429

    # Check if device already used free session today
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    free_plan = WiFiPlan.query.filter_by(is_free=True).first()
    if free_plan:
        today_session = GuestSession.query.filter(
            GuestSession.phone == phone,
            GuestSession.plan_id == free_plan.id,
            GuestSession.created_at > today_start,
            GuestSession.status.in_(['active', 'expired'])
        ).first()
        if today_session:
            return jsonify({'error': 'Free access is limited to once per day. Consider upgrading to a paid plan!'}), 429

    # Generate 6-digit OTP
    otp_code = f'{random.randint(100000, 999999)}'
    otp = OTPRequest(
        phone=phone,
        code=otp_code,
        expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    db.session.add(otp)
    db.session.commit()

    # Send SMS
    try:
        from app.services.sms import send_sms
        send_sms(
            phone=phone,
            message=f'Your Faiba WiFi verification code is: {otp_code}. Valid for 5 minutes.'
        )
    except Exception as e:
        logger.error(f'SMS send error: {e}')
        # Continue anyway - in development, log the OTP
        logger.info(f'OTP for {phone}: {otp_code}')

    return jsonify({
        'status': 'sent',
        'message': f'OTP sent to {phone}',
        'otp_id': otp.id,
        # Include OTP in response for testing/development
        'debug_otp': otp_code if current_app.config.get('MPESA_ENV') == 'sandbox' else None,
    })


@auth_bp.route('/otp/verify', methods=['POST'])
def verify_otp():
    """Verify OTP and create free WiFi session.

    Request JSON:
        phone: str
        code: str - 6-digit OTP code
        mac_address: str (optional) - Device MAC for session stacking
    """
    data = request.get_json()
    if not data or not data.get('phone') or not data.get('code'):
        return jsonify({'error': 'Phone and OTP code required'}), 400

    phone = data['phone'].strip().replace(' ', '')
    code = data['code'].strip()
    mac_address = (data.get('mac_address') or '').strip()

    otp = OTPRequest.query.filter_by(
        phone=phone,
        code=code,
        verified=False,
    ).order_by(OTPRequest.created_at.desc()).first()

    if not otp:
        return jsonify({'error': 'Invalid OTP code'}), 400

    if otp.expires_at < datetime.utcnow():
        return jsonify({'error': 'OTP has expired. Request a new one.'}), 400

    if otp.attempts >= 5:
        return jsonify({'error': 'Too many attempts. Request a new OTP.'}), 429

    otp.attempts += 1

    if otp.code != code:
        db.session.commit()
        return jsonify({'error': 'Incorrect OTP code'}), 400

    # OTP verified
    otp.verified = True
    db.session.commit()

    # Create free session
    free_plan = WiFiPlan.query.filter_by(is_free=True, active=True).first()
    if not free_plan:
        return jsonify({'error': 'Free plan not available'}), 500

    # If MAC provided, use MAC-based stacking
    mac = normalize_mac(mac_address)
    if mac:
        result = activate_mac_session(
            mac_address=mac,
            plan_id=free_plan.id,
            credit_type='free',
            phone=phone,
            venue=current_app.config['DEFAULT_VENUE'],
        )
        if result:
            return jsonify({
                'status': 'success',
                'message': f'Welcome! You have {free_plan.duration_label} of free WiFi.',
                'mac': mac,
                'session_info': result,
            })

    # Fallback: legacy session
    session = GuestSession(
        phone=phone,
        plan_id=free_plan.id,
        venue=current_app.config['DEFAULT_VENUE'],
        mac_address=mac or '',
        ip_address=request.remote_addr,
        status='pending',
    )
    db.session.add(session)
    db.session.commit()

    activate_session(session.id)

    return jsonify({
        'status': 'success',
        'message': f'Welcome! You have {free_plan.duration_label} of free WiFi.',
        'session_id': session.id,
    })


@auth_bp.route('/voucher', methods=['POST'])
def redeem_voucher():
    """Validate and redeem a voucher or promo code.

    If MAC address is provided, the voucher credit is stacked onto the
    existing MacSession, extending time and data.

    Request JSON:
        code: str - Voucher/promo code
        phone: str (optional) - For receipt
        mac_address: str (optional) - Device MAC for session stacking
    """
    data = request.get_json()
    if not data or not data.get('code'):
        return jsonify({'error': 'Voucher code required'}), 400

    code = data['code'].strip().upper()
    phone = data.get('phone', '').strip()
    mac_address = (data.get('mac_address') or '').strip()

    voucher = Voucher.query.filter_by(code=code).first()
    if not voucher:
        return jsonify({'error': 'Invalid voucher code'}), 400

    if voucher.redeemed:
        return jsonify({'error': 'This voucher has already been used'}), 400

    # Redeem voucher
    voucher.redeemed = True
    voucher.redeemed_by = phone or 'anonymous'
    voucher.redeemed_at = datetime.utcnow()
    db.session.commit()

    plan = voucher.plan

    # If MAC provided, use MAC-based stacking
    mac = normalize_mac(mac_address)
    if mac:
        result = activate_mac_session(
            mac_address=mac,
            plan_id=plan.id,
            credit_type='voucher',
            transaction_code=code,
            phone=phone,
            venue=current_app.config['DEFAULT_VENUE'],
        )
        if result:
            return jsonify({
                'status': 'success',
                'message': f'Voucher redeemed! Session {"extended" if result["credits_count"] > 1 else "activated"} — {plan.duration_label} ({plan.data_mb} MB).',
                'mac': mac,
                'session_info': result,
            })

    # Fallback: legacy session
    session = GuestSession(
        phone=phone,
        plan_id=voucher.plan_id,
        venue=current_app.config['DEFAULT_VENUE'],
        mac_address=mac or '',
        ip_address=request.remote_addr,
        status='pending',
    )
    db.session.add(session)
    db.session.commit()

    activate_session(session.id)

    return jsonify({
        'status': 'success',
        'message': f'Voucher redeemed! You have {plan.duration_label} of Faiba WiFi ({plan.data_mb} MB).',
        'session_id': session.id,
    })

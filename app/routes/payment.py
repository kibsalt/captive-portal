import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app

from app.models import db, WiFiPlan, GuestSession, Payment, MacCredit, normalize_mac
from app.services.session_manager import activate_session, activate_mac_session, activate_mac_session_from_external

logger = logging.getLogger(__name__)

payment_bp = Blueprint('payment', __name__)


@payment_bp.route('/initiate', methods=['POST'])
def initiate_payment():
    """Initiate a payment for a WiFi plan.

    Request JSON:
        plan_id: int - WiFi plan ID
        method: str - Payment method (mpesa, airtel, tkash, card, etc.)
        phone: str - Phone number or account number
        mac_address: str (optional) - Device MAC address
        ip_address: str (optional) - Device IP address
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    plan_id = data.get('plan_id')
    method = data.get('method')
    phone = data.get('phone', '').strip()
    mac_address = data.get('mac_address', '').strip()

    if not plan_id or not method:
        return jsonify({'error': 'plan_id and method are required'}), 400

    plan = WiFiPlan.query.get(plan_id)
    if not plan or not plan.active or plan.is_free:
        return jsonify({'error': 'Invalid plan'}), 400

    if method in ('mpesa', 'airtel', 'tkash', 'sms', 'pesalink') and not phone:
        return jsonify({'error': 'Phone number required for this payment method'}), 400

    mac = normalize_mac(mac_address)

    session = GuestSession(
        phone=phone,
        plan_id=plan.id,
        venue=current_app.config['DEFAULT_VENUE'],
        mac_address=mac or '',
        ip_address=data.get('ip_address', request.remote_addr),
        status='pending',
    )
    db.session.add(session)
    db.session.flush()

    payment = Payment(
        session_id=session.id,
        method=method,
        amount=plan.price,
        phone=phone,
        account_ref=f'FAIBA-{session.id[:8].upper()}',
        status='pending',
    )
    db.session.add(payment)
    db.session.commit()

    if method == 'mpesa':
        result = _initiate_mpesa(payment, phone, plan)
    elif method in ('airtel', 'tkash'):
        result = _initiate_mobile_money(payment, method, phone, plan)
    elif method == 'card':
        result = _initiate_card(payment, plan)
    elif method == 'voucher':
        return jsonify({'error': 'Use /api/auth/voucher for voucher payments'}), 400
    else:
        result = {
            'status': 'pending',
            'message': f'Payment initiated via {method}. Please confirm on your device.',
            'session_id': session.id,
            'payment_id': payment.id,
        }

    return jsonify(result)


def _initiate_mpesa(payment, phone, plan):
    """Initiate M-Pesa STK push via Lexabensa gateway."""
    try:
        from app.services.mpesa import stk_push
        result = stk_push(phone=phone, amount=plan.price)

        if result.get('success'):
            payment.status = 'pending'
            payment.status_message = 'STK push sent via Lexabensa'
            db.session.commit()
            return {
                'status': 'pending',
                'message': result['message'],
                'session_id': payment.session_id,
                'payment_id': payment.id,
                'phone': phone,
            }
        else:
            payment.status = 'failed'
            payment.status_message = result.get('message', 'STK push failed')
            db.session.commit()
            return {'status': 'failed', 'message': result.get('message', 'STK push failed')}

    except Exception as e:
        logger.error(f'M-Pesa STK push error: {e}')
        payment.status = 'failed'
        payment.status_message = str(e)
        db.session.commit()
        return {'status': 'failed', 'message': f'Payment error: {e}'}


def _initiate_mobile_money(payment, method, phone, plan):
    payment.status = 'pending'
    payment.status_message = f'{method} payment prompt sent'
    db.session.commit()
    return {
        'status': 'pending',
        'message': f'Payment prompt sent to {phone}. Confirm on your device.',
        'session_id': payment.session_id,
        'payment_id': payment.id,
    }


def _initiate_card(payment, plan):
    payment.status = 'pending'
    payment.status_message = 'Awaiting card payment'
    db.session.commit()
    return {
        'status': 'pending',
        'message': 'Card payment gateway initiated.',
        'session_id': payment.session_id,
        'payment_id': payment.id,
    }


@payment_bp.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """M-Pesa Daraja STK push callback (webhook)."""
    data = request.get_json()
    logger.info(f'M-Pesa callback received: {data}')

    if not data:
        return jsonify({'ResultCode': 1, 'ResultDesc': 'No data'}), 400

    body = data.get('Body', {}).get('stkCallback', {})
    checkout_request_id = body.get('CheckoutRequestID')
    result_code = body.get('ResultCode')

    if not checkout_request_id:
        return jsonify({'ResultCode': 1, 'ResultDesc': 'Missing CheckoutRequestID'}), 400

    payment = Payment.query.filter_by(checkout_request_id=checkout_request_id).first()
    if not payment:
        logger.warning(f'Payment not found for CheckoutRequestID: {checkout_request_id}')
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})

    if result_code == 0:
        metadata = body.get('CallbackMetadata', {}).get('Item', [])
        mpesa_receipt = next((item['Value'] for item in metadata if item['Name'] == 'MpesaReceiptNumber'), None)

        payment.status = 'completed'
        payment.transaction_id = mpesa_receipt
        payment.completed_at = datetime.utcnow()
        payment.status_message = 'Payment confirmed'
        db.session.commit()

        activate_session(payment.session_id)
        logger.info(f'M-Pesa payment completed: {mpesa_receipt} for session {payment.session_id}')
    else:
        payment.status = 'failed'
        payment.status_message = body.get('ResultDesc', 'Payment failed')
        db.session.commit()
        logger.info(f'M-Pesa payment failed: {body.get("ResultDesc")}')

    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@payment_bp.route('/mpesa/check/<int:payment_id>')
def mpesa_check(payment_id):
    """Poll M-Pesa payment status via Lexabensa API.

    Called by frontend every 2s after STK push.
    Lexabensa API: GET https://lexabensa.com/api/?code=<phone>
    Returns: voucher,upspeed,downspeed,downlimit,amount,session_end,start_date

    If payment found, activates session using Lexabensa QoS values
    (upspeed, downspeed, downlimit) rather than local plan data.
    """
    payment = Payment.query.get(payment_id)
    if not payment:
        return jsonify({'error': 'Payment not found'}), 404

    if payment.status == 'completed':
        return jsonify({
            'status': 'completed',
            'mpesa_code': payment.transaction_id,
            'session_id': payment.session_id,
        })

    if payment.status == 'failed':
        return jsonify({'status': 'failed', 'message': payment.status_message})

    phone = payment.phone
    if not phone:
        return jsonify({'status': 'pending', 'message': 'Waiting for payment...'})

    # Call Lexabensa API with phone number
    from app.services.mpesa import check_payment
    result = check_payment(phone)

    if not result.get('found'):
        return jsonify({'status': 'pending', 'message': 'Waiting for M-Pesa confirmation...'})

    paid_amount = result.get('amount', 0)
    if paid_amount < payment.amount:
        return jsonify({
            'status': 'pending',
            'message': f'Payment of KES {paid_amount} found but KES {payment.amount} required.',
        })

    voucher_code = result['voucher']

    # Mark payment as completed
    payment.status = 'completed'
    payment.transaction_id = voucher_code
    payment.completed_at = datetime.utcnow()
    payment.status_message = f'M-Pesa confirmed: {voucher_code} (KES {paid_amount})'
    db.session.commit()

    # Activate using Lexabensa QoS data
    mac = normalize_mac(payment.session.mac_address) if payment.session else None
    if mac:
        activate_mac_session_from_external(
            mac_address=mac,
            voucher_code=voucher_code,
            phone=phone,
            amount=paid_amount,
            upspeed_kbps=result.get('upspeed', 0),
            downspeed_kbps=result.get('downspeed', 0),
            downlimit_bytes=result.get('downlimit', 0),
            session_end=result.get('session_end', 0),
            venue=current_app.config['DEFAULT_VENUE'],
        )
    else:
        activate_session(payment.session_id)

    logger.info(f'M-Pesa confirmed: {voucher_code} KES {paid_amount} for {phone}')

    return jsonify({
        'status': 'completed',
        'mpesa_code': voucher_code,
        'amount': paid_amount,
        'session_id': payment.session_id,
        'message': f'Payment confirmed! Receipt: {voucher_code}',
    })


@payment_bp.route('/mpesa/verify-code', methods=['POST'])
def mpesa_verify_code():
    """Verify an M-Pesa confirmation code entered manually.

    Calls the same Lexabensa API but with the M-Pesa code instead of phone:
        GET https://lexabensa.com/api/?code=UD2GEB7XGS

    If found, activates session using the returned QoS values.

    Request JSON:
        mpesa_code: str - M-Pesa confirmation code
        phone: str - Phone number used for payment
        plan_id: int - WiFi plan ID
        mac_address: str (optional) - Device MAC address
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    mpesa_code = (data.get('mpesa_code') or '').strip().upper()
    phone = (data.get('phone') or '').strip()
    plan_id = data.get('plan_id')
    mac_address = (data.get('mac_address') or '').strip()

    if not mpesa_code:
        return jsonify({'error': 'M-Pesa confirmation code is required'}), 400
    if not phone:
        return jsonify({'error': 'Phone number is required'}), 400
    if not plan_id:
        return jsonify({'error': 'Please select a data plan'}), 400

    plan = WiFiPlan.query.get(plan_id)
    if not plan or not plan.active or plan.is_free:
        return jsonify({'error': 'Invalid plan'}), 400

    # Check if already used locally
    existing_payment = Payment.query.filter_by(transaction_id=mpesa_code, status='completed').first()
    existing_credit = MacCredit.query.filter_by(transaction_code=mpesa_code).first()
    if existing_payment or existing_credit:
        return jsonify({'error': 'This M-Pesa code has already been used'}), 400

    # Look up by M-Pesa code via Lexabensa API
    from app.services.mpesa import check_mpesa_code, check_payment
    result = check_mpesa_code(mpesa_code)

    # If not found by code, try by phone number and verify the code matches
    if not result.get('found'):
        result = check_payment(phone)
        if result.get('found') and result.get('voucher', '').upper() != mpesa_code:
            result = {'found': False}

    if not result.get('found'):
        return jsonify({
            'error': 'No M-Pesa payment found. Please check the code and phone number.'
        }), 400

    paid_amount = result['amount']
    voucher_code = result['voucher']

    if paid_amount < plan.price:
        return jsonify({
            'error': f'Payment of KES {paid_amount} found but KES {plan.price} is required for this plan.'
        }), 400

    mac = normalize_mac(mac_address)

    if mac:
        mac_result = activate_mac_session_from_external(
            mac_address=mac,
            voucher_code=voucher_code,
            phone=phone,
            amount=paid_amount,
            upspeed_kbps=result.get('upspeed', 0),
            downspeed_kbps=result.get('downspeed', 0),
            downlimit_bytes=result.get('downlimit', 0),
            session_end=result.get('session_end', 0),
            venue=current_app.config['DEFAULT_VENUE'],
        )
        if not mac_result:
            return jsonify({'error': 'Failed to activate session. Code may already be used.'}), 400

        logger.info(f'M-Pesa code verified: {voucher_code} KES {paid_amount} for {phone} → {mac}')
        return jsonify({
            'status': 'success',
            'mpesa_code': voucher_code,
            'amount': paid_amount,
            'mac': mac,
            'session_info': mac_result,
            'message': f'Payment verified! Receipt: {voucher_code}. Session {"extended" if mac_result["credits_count"] > 1 else "activated"}.',
        })

    # No MAC — legacy flow
    session = GuestSession(
        phone=phone, plan_id=plan.id,
        venue=current_app.config['DEFAULT_VENUE'],
        ip_address=request.remote_addr, status='pending',
    )
    db.session.add(session)
    db.session.flush()
    payment = Payment(
        session_id=session.id, method='mpesa', amount=plan.price,
        phone=phone, account_ref=f'FAIBA-{session.id[:8].upper()}',
        transaction_id=voucher_code, status='completed',
        completed_at=datetime.utcnow(),
        status_message=f'M-Pesa code verified: {voucher_code} (KES {paid_amount})',
    )
    db.session.add(payment)
    db.session.commit()
    activate_session(session.id)

    logger.info(f'M-Pesa code verified (legacy): {voucher_code} KES {paid_amount} for {phone}')
    return jsonify({
        'status': 'success',
        'mpesa_code': voucher_code,
        'amount': paid_amount,
        'session_id': session.id,
        'message': f'Payment verified! Receipt: {voucher_code}. Your session is now active.',
    })


@payment_bp.route('/confirm', methods=['POST'])
def confirm_payment():
    """Manual payment confirmation (for testing or bank confirmations)."""
    data = request.get_json()
    if not data or not data.get('payment_id'):
        return jsonify({'error': 'payment_id required'}), 400

    payment = Payment.query.get(data['payment_id'])
    if not payment:
        return jsonify({'error': 'Payment not found'}), 404

    if payment.status == 'completed':
        return jsonify({'status': 'already_completed', 'session_id': payment.session_id})

    payment.status = 'completed'
    payment.transaction_id = data.get('transaction_id', f'MANUAL-{payment.id}')
    payment.completed_at = datetime.utcnow()
    payment.status_message = 'Manually confirmed'
    db.session.commit()

    activate_session(payment.session_id)

    return jsonify({
        'status': 'success',
        'message': 'Payment confirmed and session activated',
        'session_id': payment.session_id,
    })


@payment_bp.route('/status/<int:payment_id>')
def payment_status(payment_id):
    """Check payment status."""
    payment = Payment.query.get(payment_id)
    if not payment:
        return jsonify({'error': 'Payment not found'}), 404

    return jsonify({
        'payment_id': payment.id,
        'status': payment.status,
        'message': payment.status_message,
        'session_id': payment.session_id,
        'session_status': payment.session.status if payment.session else None,
    })

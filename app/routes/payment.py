import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app

from app.models import db, WiFiPlan, GuestSession, Payment, MacCredit, normalize_mac
from app.services.session_manager import activate_session, activate_mac_session

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

    # Normalize MAC if provided
    mac = normalize_mac(mac_address)

    # Create guest session
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

    # Create payment record
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

    # Dispatch to payment provider
    if method == 'mpesa':
        result = _initiate_mpesa(payment, phone, plan)
    elif method in ('airtel', 'tkash'):
        result = _initiate_mobile_money(payment, method, phone, plan)
    elif method == 'card':
        result = _initiate_card(payment, plan)
    elif method == 'voucher':
        return jsonify({'error': 'Use /api/auth/voucher for voucher payments'}), 400
    else:
        # For bank/PesaLink/SMS - simulate pending
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
    """Stub for Airtel Money / T-Kash."""
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
    """Stub for card payment - would redirect to payment gateway."""
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
        # Payment successful
        metadata = body.get('CallbackMetadata', {}).get('Item', [])
        mpesa_receipt = next((item['Value'] for item in metadata if item['Name'] == 'MpesaReceiptNumber'), None)

        payment.status = 'completed'
        payment.transaction_id = mpesa_receipt
        payment.completed_at = datetime.utcnow()
        payment.status_message = 'Payment confirmed'
        db.session.commit()

        # Activate the WiFi session
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
    """Check M-Pesa payment status — polls both Lexabensa API and external MySQL DB.

    Called by frontend polling after STK push is sent.
    Checks the external voucher DB (SHA256 phone hash lookup) first,
    then falls back to Lexabensa API.
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

    # --- Check external MySQL voucher DB first (SHA256 hash lookup) ---
    try:
        from app.services.external_vouchers import lookup_by_phone
        ext = lookup_by_phone(phone, current_app.config)

        if ext and ext.get('voucher'):
            paid_amount = ext['amount']
            mpesa_code = ext['voucher']

            if paid_amount >= payment.amount:
                # Payment confirmed from external DB
                payment.status = 'completed'
                payment.transaction_id = mpesa_code
                payment.completed_at = datetime.utcnow()
                payment.status_message = f'M-Pesa confirmed (external DB): {mpesa_code} (KES {paid_amount})'
                db.session.commit()

                # Activate using external DB QoS if MAC available
                mac = normalize_mac(payment.session.mac_address) if payment.session else None
                if mac:
                    from app.services.session_manager import activate_mac_session_from_external
                    activate_mac_session_from_external(
                        mac_address=mac,
                        voucher_code=mpesa_code,
                        phone=phone,
                        amount=paid_amount,
                        upspeed_kbps=ext['upspeed'],
                        downspeed_kbps=ext['downspeed'],
                        downlimit_bytes=ext['downlimit'],
                        session_end=ext['session_end'],
                        venue=current_app.config['DEFAULT_VENUE'],
                    )
                else:
                    activate_session(payment.session_id)

                logger.info(f'M-Pesa confirmed (external DB): {mpesa_code} KES {paid_amount} for {phone}')

                return jsonify({
                    'status': 'completed',
                    'mpesa_code': mpesa_code,
                    'amount': paid_amount,
                    'session_id': payment.session_id,
                    'message': f'Payment confirmed! Receipt: {mpesa_code}',
                })
    except Exception as e:
        logger.warning(f'External DB check failed during polling, falling back to Lexabensa: {e}')

    # --- Fallback: check Lexabensa API ---
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

    # Payment confirmed via Lexabensa
    payment.status = 'completed'
    payment.transaction_id = result.get('mpesa_code', '')
    payment.completed_at = datetime.utcnow()
    payment.status_message = f'M-Pesa confirmed: {result.get("mpesa_code")} (KES {paid_amount})'
    db.session.commit()

    activate_session(payment.session_id)

    logger.info(f'M-Pesa payment confirmed (Lexabensa): {result.get("mpesa_code")} KES {paid_amount} for {phone}')

    return jsonify({
        'status': 'completed',
        'mpesa_code': result.get('mpesa_code'),
        'amount': paid_amount,
        'session_id': payment.session_id,
        'message': f'Payment confirmed! Receipt: {result.get("mpesa_code")}',
    })


@payment_bp.route('/mpesa/verify-code', methods=['POST'])
def mpesa_verify_code():
    """Verify an M-Pesa confirmation code entered manually by the user.

    This allows users who already paid (or whose STK push failed) to enter
    their M-Pesa transaction code to activate their WiFi session.

    If a MAC address is provided, the payment is stacked onto the MacSession,
    allowing multiple payments to extend the same session.

    Request JSON:
        mpesa_code: str - M-Pesa confirmation code (e.g. SJ12ABC345)
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

    # Check if this M-Pesa code was already used locally
    existing_payment = Payment.query.filter_by(transaction_id=mpesa_code, status='completed').first()
    existing_credit = MacCredit.query.filter_by(transaction_code=mpesa_code).first()
    if existing_payment or existing_credit:
        return jsonify({'error': 'This M-Pesa code has already been used'}), 400

    mac = normalize_mac(mac_address)
    ext = None
    paid_amount = 0

    # --- Strategy 1: Look up by M-Pesa code in external MySQL DB ---
    try:
        from app.services.external_vouchers import lookup_by_mpesa_code, lookup_by_phone
        ext = lookup_by_mpesa_code(mpesa_code, current_app.config)

        # If not found by code, try by phone hash
        if not ext:
            ext = lookup_by_phone(phone, current_app.config)
            # Verify the code matches
            if ext and ext.get('voucher', '').upper() != mpesa_code:
                ext = None
    except Exception as e:
        logger.warning(f'External DB lookup failed, falling back to Lexabensa: {e}')

    if ext:
        paid_amount = ext['amount']

        if paid_amount < plan.price:
            return jsonify({
                'error': f'Payment of KES {paid_amount} found but KES {plan.price} is required for this plan.'
            }), 400

        # Activate using external DB QoS
        if mac:
            from app.services.session_manager import activate_mac_session_from_external
            mac_result = activate_mac_session_from_external(
                mac_address=mac,
                voucher_code=mpesa_code,
                phone=phone,
                amount=paid_amount,
                upspeed_kbps=ext['upspeed'],
                downspeed_kbps=ext['downspeed'],
                downlimit_bytes=ext['downlimit'],
                session_end=ext['session_end'],
                venue=current_app.config['DEFAULT_VENUE'],
            )
            if not mac_result:
                return jsonify({'error': 'Failed to activate session. Code may already be used.'}), 400

            logger.info(f'M-Pesa code verified (external DB, MAC): {mpesa_code} KES {paid_amount} → {mac}')

            return jsonify({
                'status': 'success',
                'mpesa_code': mpesa_code,
                'amount': paid_amount,
                'mac': mac,
                'session_info': mac_result,
                'message': f'Payment verified! Receipt: {mpesa_code}. Session {"extended" if mac_result["credits_count"] > 1 else "activated"}.',
            })
        else:
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
                transaction_id=mpesa_code, status='completed',
                completed_at=datetime.utcnow(),
                status_message=f'M-Pesa code verified (external DB): {mpesa_code} (KES {paid_amount})',
            )
            db.session.add(payment)
            db.session.commit()
            activate_session(session.id)
            logger.info(f'M-Pesa code verified (external DB, legacy): {mpesa_code} KES {paid_amount}')
            return jsonify({
                'status': 'success', 'mpesa_code': mpesa_code, 'amount': paid_amount,
                'session_id': session.id,
                'message': f'Payment verified! Receipt: {mpesa_code}. Your session is now active.',
            })

    # --- Strategy 2: Fallback to Lexabensa API ---
    from app.services.mpesa import check_payment
    result = check_payment(phone)

    if not result.get('found'):
        return jsonify({
            'error': 'No M-Pesa payment found for this phone number. Please check and try again.'
        }), 400

    api_code = (result.get('mpesa_code') or '').strip().upper()
    if api_code and api_code != mpesa_code:
        return jsonify({
            'error': 'M-Pesa code does not match the latest payment. Please verify the code.'
        }), 400

    paid_amount = result.get('amount', 0)
    if paid_amount < plan.price:
        return jsonify({
            'error': f'Payment of KES {paid_amount} found but KES {plan.price} is required for this plan.'
        }), 400

    # Activate via Lexabensa data
    if mac:
        mac_result = activate_mac_session(
            mac_address=mac, plan_id=plan.id, credit_type='mpesa',
            transaction_code=mpesa_code, phone=phone, amount_paid=paid_amount,
            venue=current_app.config['DEFAULT_VENUE'],
        )
        if not mac_result:
            return jsonify({'error': 'Failed to activate session. Code may already be used.'}), 400

        logger.info(f'M-Pesa code verified (Lexabensa, MAC): {mpesa_code} KES {paid_amount} → {mac}')
        return jsonify({
            'status': 'success', 'mpesa_code': mpesa_code, 'amount': paid_amount,
            'mac': mac, 'session_info': mac_result,
            'message': f'Payment verified! Receipt: {mpesa_code}. Session {"extended" if mac_result["credits_count"] > 1 else "activated"}.',
        })

    # No MAC — legacy
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
        transaction_id=mpesa_code, status='completed',
        completed_at=datetime.utcnow(),
        status_message=f'M-Pesa code verified (Lexabensa): {mpesa_code} (KES {paid_amount})',
    )
    db.session.add(payment)
    db.session.commit()
    activate_session(session.id)
    logger.info(f'M-Pesa code verified (Lexabensa, legacy): {mpesa_code} KES {paid_amount}')
    return jsonify({
        'status': 'success', 'mpesa_code': mpesa_code, 'amount': paid_amount,
        'session_id': session.id,
        'message': f'Payment verified! Receipt: {mpesa_code}. Your session is now active.',
    })


@payment_bp.route('/confirm', methods=['POST'])
def confirm_payment():
    """Manual payment confirmation (for testing or bank confirmations).

    Request JSON:
        payment_id: int
        transaction_id: str (optional)
    """
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

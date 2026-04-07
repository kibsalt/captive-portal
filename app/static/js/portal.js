// Faiba WiFi Captive Portal - Frontend Logic

const API_BASE = '';

// --- State ---
let selectedPlan = null;
let selectedPay = 'mpesa';
let currentSessionId = null;
let pollTimer = null;

// MAC address from server (injected by BRAS redirect query param)
const deviceMac = (window.PORTAL_CONFIG && window.PORTAL_CONFIG.mac_address) || '';

// --- Payment method configs ---
const payConfigs = {
  mpesa:    { label:'M-Pesa Phone Number',    hint:'You will receive an M-Pesa STK push prompt on your phone', placeholder:'07XX XXX XXX', type:'tel' },
  airtel:   { label:'Airtel Money Number',     hint:'An Airtel Money payment prompt will be sent to your phone', placeholder:'07XX XXX XXX', type:'tel' },
  tkash:    { label:'Telkom T-Kash Number',    hint:'A T-Kash payment prompt will be sent to your phone', placeholder:'07XX XXX XXX', type:'tel' },
  equity:   { label:'Equity Bank Account No.', hint:'Funds will be debited from your Equity Bank account via PesaLink', placeholder:'0110XXXXXXXXX', type:'tel' },
  kcb:      { label:'KCB Account Number',      hint:'Funds will be debited from your KCB account via PesaLink', placeholder:'KCB account number', type:'text' },
  coop:     { label:'Co-op Bank Account No.',  hint:'Funds will be debited from your Co-op Bank account via PesaLink', placeholder:'Co-op account number', type:'text' },
  ncba:     { label:'NCBA Account Number',     hint:'Funds will be debited from your NCBA account via PesaLink', placeholder:'NCBA account number', type:'text' },
  stanbic:  { label:'Stanbic Account Number',  hint:'Funds will be debited from your Stanbic account via PesaLink', placeholder:'Stanbic account number', type:'text' },
  pesalink: { label:'PesaLink Phone Number',   hint:'Enter the phone number linked to your bank account', placeholder:'07XX XXX XXX', type:'tel' },
  card:     { label:'Card Number',             hint:'Visa, Mastercard, or American Express accepted. Secured by 3D Secure.', placeholder:'XXXX XXXX XXXX XXXX', type:'text' },
  voucher:  { label:'Voucher / Promo Code',    hint:'Enter your prepaid voucher or promotional code', placeholder:'FAIBA-XXXX-XXXX', type:'text' },
  sms:      { label:'Phone Number for SMS PIN',hint:'An SMS PIN will be sent. Reply with PIN to confirm payment.', placeholder:'07XX XXX XXX', type:'tel' },
};

// --- Tab switching ---
function switchTab(tab, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-' + tab).classList.add('active');
}

// --- Package selection ---
function selectPkg(el) {
  document.querySelectorAll('.pkg').forEach(p => p.classList.remove('selected'));
  el.classList.add('selected');
  selectedPlan = {
    id: parseInt(el.dataset.planId),
    price: el.dataset.price,
    label: el.dataset.label,
  };
  document.getElementById('total-display').textContent = 'KES ' + el.dataset.price;
}

// --- Payment method selection ---
function selectPay(el, method) {
  document.querySelectorAll('.pay-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  selectedPay = method;
  const cfg = payConfigs[method];
  document.getElementById('input-label').textContent = cfg.label;
  document.getElementById('input-hint').textContent = cfg.hint;
  const inp = document.getElementById('pay-input');
  inp.placeholder = cfg.placeholder;
  inp.type = cfg.type;
  inp.value = '';

  // Show/hide M-Pesa code section
  const mpesaSection = document.getElementById('mpesa-code-section');
  if (method === 'mpesa') {
    mpesaSection.style.display = 'block';
  } else {
    mpesaSection.style.display = 'none';
    // Reset M-Pesa code fields when switching away
    document.getElementById('mpesa-code-fields').classList.remove('visible');
    document.getElementById('mpesa-code-toggle').classList.add('visible');
  }
}

// --- Toggle M-Pesa code input ---
function toggleMpesaCode() {
  const fields = document.getElementById('mpesa-code-fields');
  const toggle = document.getElementById('mpesa-code-toggle');
  if (fields.classList.contains('visible')) {
    fields.classList.remove('visible');
    toggle.textContent = 'Already paid? Enter M-Pesa confirmation code';
  } else {
    fields.classList.add('visible');
    toggle.textContent = 'Hide M-Pesa code input';
    // Auto-focus the code input
    setTimeout(() => document.getElementById('mpesa-code-input').focus(), 100);
  }
}

// --- Handle M-Pesa confirmation code ---
async function handleMpesaCode() {
  const codeInput = document.getElementById('mpesa-code-input');
  const phoneInput = document.getElementById('mpesa-code-phone');
  const code = codeInput.value.trim().toUpperCase();
  const phone = phoneInput.value.trim();

  if (!code) {
    codeInput.focus();
    showToast('Please enter your M-Pesa confirmation code', 'error');
    return;
  }

  if (!phone) {
    phoneInput.focus();
    showToast('Please enter the phone number used for payment', 'error');
    return;
  }

  if (!selectedPlan) {
    showToast('Please select a data plan', 'error');
    return;
  }

  setLoading('btn-mpesa-code', true);

  try {
    const response = await fetch(API_BASE + '/api/payment/mpesa/verify-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mpesa_code: code,
        phone: phone,
        plan_id: selectedPlan.id,
        mac_address: deviceMac,
      }),
    });

    const data = await response.json();

    if (data.status === 'success' || data.status === 'completed') {
      currentSessionId = data.session_id;
      showSuccess(
        "You're Connected!",
        data.message || 'M-Pesa payment verified. Enjoy Faiba WiFi!'
      );
    } else {
      showToast(data.error || data.message || 'Could not verify M-Pesa code', 'error');
    }
  } catch (err) {
    showToast('Network error. Please try again.', 'error');
  }

  setLoading('btn-mpesa-code', false);
}

// --- Toast notifications ---
function showToast(message, type) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();

  const toast = document.createElement('div');
  toast.className = 'toast' + (type === 'success' ? ' success' : '');
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

// --- Show success overlay ---
function showSuccess(message, details) {
  document.getElementById('success-msg').textContent = message;
  const detailsEl = document.getElementById('success-details');
  if (detailsEl) detailsEl.textContent = details || '';
  document.getElementById('success-overlay').classList.add('show');
}

function closeSuccess() {
  document.getElementById('success-overlay').classList.remove('show');
}

// --- Set button loading state ---
function setLoading(btnId, loading) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  if (loading) {
    btn.disabled = true;
    btn.dataset.originalText = btn.textContent;
    btn.innerHTML = '<span class="spinner"></span>Processing...';
  } else {
    btn.disabled = false;
    btn.textContent = btn.dataset.originalText || btn.textContent;
  }
}

// --- Handle paid plan payment ---
async function handlePay() {
  const inp = document.getElementById('pay-input');
  const phone = inp.value.trim();

  if (!phone) {
    inp.focus();
    showToast('Please enter your ' + payConfigs[selectedPay].label.toLowerCase(), 'error');
    return;
  }

  if (!selectedPlan) {
    showToast('Please select a data plan', 'error');
    return;
  }

  // Voucher goes through a different endpoint
  if (selectedPay === 'voucher') {
    return handleVoucher(phone);
  }

  setLoading('btn-pay', true);

  try {
    const response = await fetch(API_BASE + '/api/payment/initiate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        plan_id: selectedPlan.id,
        method: selectedPay,
        phone: phone,
        mac_address: deviceMac,
      }),
    });

    const data = await response.json();

    if (!response.ok) {
      showToast(data.error || 'Payment failed', 'error');
      setLoading('btn-pay', false);
      return;
    }

    if (data.status === 'pending') {
      showToast(data.message, 'success');
      currentSessionId = data.session_id;
      // Start polling for payment confirmation
      if (selectedPay === 'mpesa') {
        startMpesaPolling(data.payment_id);
      } else {
        startPaymentPolling(data.payment_id);
      }
    } else if (data.status === 'failed') {
      showToast(data.message || 'Payment failed', 'error');
    }
  } catch (err) {
    showToast('Network error. Please try again.', 'error');
  }

  setLoading('btn-pay', false);
}

// --- Poll M-Pesa payment via Lexabensa verification ---
function startMpesaPolling(paymentId) {
  if (pollTimer) clearInterval(pollTimer);

  let attempts = 0;
  pollTimer = setInterval(async () => {
    attempts++;
    if (attempts > 90) { // 3 minutes max
      clearInterval(pollTimer);
      setLoading('btn-pay', false);
      showToast('Payment timeout. Check your M-Pesa and try again.', 'error');
      return;
    }

    try {
      const response = await fetch(API_BASE + '/api/payment/mpesa/check/' + paymentId);
      const data = await response.json();

      if (data.status === 'completed') {
        clearInterval(pollTimer);
        setLoading('btn-pay', false);
        showSuccess(
          "You're Connected!",
          data.message || 'M-Pesa payment confirmed. Enjoy Faiba WiFi!'
        );
      } else if (data.status === 'failed') {
        clearInterval(pollTimer);
        setLoading('btn-pay', false);
        showToast(data.message || 'Payment failed', 'error');
      }
      // 'pending' — keep polling
    } catch (err) {
      // Silently retry
    }
  }, 2000);
}

// --- Poll for payment status (non-mpesa) ---
function startPaymentPolling(paymentId) {
  if (pollTimer) clearInterval(pollTimer);

  let attempts = 0;
  pollTimer = setInterval(async () => {
    attempts++;
    if (attempts > 60) { // 2 minutes max
      clearInterval(pollTimer);
      showToast('Payment timeout. Check your phone and try again.', 'error');
      return;
    }

    try {
      const response = await fetch(API_BASE + '/api/payment/status/' + paymentId);
      const data = await response.json();

      if (data.status === 'completed') {
        clearInterval(pollTimer);
        showSuccess(
          "You're Connected!",
          'Payment confirmed. Your session is now active. Enjoy Faiba WiFi!'
        );
      } else if (data.status === 'failed') {
        clearInterval(pollTimer);
        showToast(data.message || 'Payment failed', 'error');
      }
    } catch (err) {
      // Silently retry
    }
  }, 2000);
}

// --- Handle voucher redemption from payment tab ---
async function handleVoucher(code) {
  setLoading('btn-pay', true);

  try {
    const response = await fetch(API_BASE + '/api/auth/voucher', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code, mac_address: deviceMac }),
    });

    const data = await response.json();

    if (data.status === 'success') {
      currentSessionId = data.session_id;
      showSuccess("You're Connected!", data.message);
    } else {
      showToast(data.error || 'Invalid voucher', 'error');
    }
  } catch (err) {
    showToast('Network error. Please try again.', 'error');
  }

  setLoading('btn-pay', false);
}

// --- Handle free OTP ---
async function handleFree() {
  const inp = document.getElementById('free-phone');
  const phone = inp.value.trim();

  if (!phone) {
    inp.focus();
    showToast('Please enter your phone number', 'error');
    return;
  }

  setLoading('btn-free', true);

  try {
    const response = await fetch(API_BASE + '/api/auth/otp/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: phone }),
    });

    const data = await response.json();

    if (data.status === 'sent') {
      // Show OTP verification UI
      document.getElementById('otp-send-section').style.display = 'none';
      document.getElementById('otp-verify-section').style.display = 'block';
      showToast('OTP sent to ' + phone, 'success');

      // In sandbox mode, auto-fill OTP
      if (data.debug_otp) {
        document.getElementById('otp-code').value = data.debug_otp;
      }
    } else {
      showToast(data.error || 'Failed to send OTP', 'error');
    }
  } catch (err) {
    showToast('Network error. Please try again.', 'error');
  }

  setLoading('btn-free', false);
}

// --- Verify OTP ---
async function verifyOTP() {
  const phone = document.getElementById('free-phone').value.trim();
  const code = document.getElementById('otp-code').value.trim();

  if (!code) {
    document.getElementById('otp-code').focus();
    showToast('Please enter the OTP code', 'error');
    return;
  }

  setLoading('btn-verify-otp', true);

  try {
    const response = await fetch(API_BASE + '/api/auth/otp/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: phone, code: code, mac_address: deviceMac }),
    });

    const data = await response.json();

    if (data.status === 'success') {
      currentSessionId = data.session_id;
      showSuccess("You're Connected!", data.message);
    } else {
      showToast(data.error || 'OTP verification failed', 'error');
    }
  } catch (err) {
    showToast('Network error. Please try again.', 'error');
  }

  setLoading('btn-verify-otp', false);
}

// --- Handle access code tab ---
async function handleCode() {
  const codeInput = document.getElementById('code-input');
  const code = codeInput.value.trim();
  const phone = document.getElementById('code-phone').value.trim();

  if (!code) {
    codeInput.focus();
    showToast('Please enter your access code', 'error');
    return;
  }

  setLoading('btn-code', true);

  try {
    const response = await fetch(API_BASE + '/api/auth/voucher', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code, phone: phone, mac_address: deviceMac }),
    });

    const data = await response.json();

    if (data.status === 'success') {
      currentSessionId = data.session_id;
      showSuccess("You're Connected!", data.message);
    } else {
      showToast(data.error || 'Invalid access code', 'error');
    }
  } catch (err) {
    showToast('Network error. Please try again.', 'error');
  }

  setLoading('btn-code', false);
}

// --- Initialize on page load ---
document.addEventListener('DOMContentLoaded', () => {
  // Select first plan
  const firstPkg = document.querySelector('.pkg');
  if (firstPkg) {
    firstPkg.classList.add('selected');
    selectedPlan = {
      id: parseInt(firstPkg.dataset.planId),
      price: firstPkg.dataset.price,
      label: firstPkg.dataset.label,
    };
  }

  // Show M-Pesa code section by default (mpesa is default payment method)
  const mpesaSection = document.getElementById('mpesa-code-section');
  if (mpesaSection) {
    mpesaSection.style.display = 'block';
  }
});

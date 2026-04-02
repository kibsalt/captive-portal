"""RADIUS Change of Authorization (CoA) and Disconnect-Message client.

Implements RFC 5176 for dynamic session control:
- CoA-Request: Change session attributes (unlock internet, apply QoS)
- Disconnect-Message: Terminate a session (on expiry or admin action)
"""

import logging
import struct
import hashlib
import socket
import os

logger = logging.getLogger(__name__)

# RADIUS packet codes
COA_REQUEST = 43
DISCONNECT_REQUEST = 40

# RADIUS attribute types
ATTRIBUTE_USER_NAME = 1
ATTRIBUTE_NAS_IP_ADDRESS = 4
ATTRIBUTE_FILTER_ID = 11
ATTRIBUTE_CLASS = 25
ATTRIBUTE_SESSION_TIMEOUT = 27
ATTRIBUTE_IDLE_TIMEOUT = 28
ATTRIBUTE_ACCT_SESSION_ID = 44
ATTRIBUTE_NAS_IDENTIFIER = 32
ATTRIBUTE_EVENT_TIMESTAMP = 55

# WISPr vendor-specific attributes (WISPr = 14122)
WISPR_VENDOR_ID = 14122
WISPR_BANDWIDTH_MAX_UP = 7
WISPR_BANDWIDTH_MAX_DOWN = 8

# Mikrotik vendor-specific (14988)
MIKROTIK_VENDOR_ID = 14988
MIKROTIK_TOTAL_LIMIT = 17


def _build_radius_packet(code, identifier, attributes, secret):
    """Build a raw RADIUS packet."""
    # Build attributes buffer
    attrs_buf = b''
    for attr_type, attr_value in attributes:
        if isinstance(attr_value, str):
            attr_value = attr_value.encode()
        attr_len = len(attr_value) + 2
        attrs_buf += struct.pack('!BB', attr_type, attr_len) + attr_value

    # Packet: code(1) + id(1) + length(2) + authenticator(16) + attributes
    length = 20 + len(attrs_buf)
    authenticator = os.urandom(16)

    packet = struct.pack('!BBH', code, identifier, length) + authenticator + attrs_buf

    # Calculate Response Authenticator = MD5(Code+ID+Length+RequestAuth+Attributes+Secret)
    md5 = hashlib.md5()
    md5.update(packet + secret)
    response_auth = md5.digest()

    # Replace authenticator
    packet = struct.pack('!BBH', code, identifier, length) + response_auth + attrs_buf

    return packet


def _build_vsa(vendor_id, attr_type, attr_value):
    """Build a Vendor-Specific Attribute (type 26)."""
    if isinstance(attr_value, int):
        attr_value = struct.pack('!I', attr_value)
    elif isinstance(attr_value, str):
        attr_value = attr_value.encode()

    # VSA: vendor_id(4) + vendor_type(1) + vendor_length(1) + value
    vsa_data = struct.pack('!IBB', vendor_id, attr_type, len(attr_value) + 2) + attr_value
    # Wrap in RADIUS attribute type 26
    total_len = len(vsa_data) + 2
    return struct.pack('!BB', 26, total_len) + vsa_data


def send_coa(server, port, secret, session_id, nas_ip, session_timeout,
             speed_down_kbps, speed_up_kbps, data_limit_bytes, session_class):
    """Send RADIUS CoA-Request to unlock internet access.

    Args:
        server: RADIUS server IP
        port: CoA port (usually 3799)
        secret: RADIUS shared secret (bytes)
        session_id: RADIUS Acct-Session-Id
        nas_ip: NAS IP address
        session_timeout: Session timeout in seconds
        speed_down_kbps: Max downstream bandwidth in kbps
        speed_up_kbps: Max upstream bandwidth in kbps
        data_limit_bytes: Total data quota in bytes
        session_class: QoS class string (e.g. 'GUEST-HOURLY')
    """
    attributes = [
        (ATTRIBUTE_ACCT_SESSION_ID, session_id),
        (ATTRIBUTE_NAS_IP_ADDRESS, socket.inet_aton(nas_ip)),
        (ATTRIBUTE_SESSION_TIMEOUT, struct.pack('!I', session_timeout)),
        (ATTRIBUTE_IDLE_TIMEOUT, struct.pack('!I', 600)),  # 10 min idle timeout
        (ATTRIBUTE_CLASS, session_class),
        (ATTRIBUTE_FILTER_ID, 'GUEST-INTERNET-ACCESS'),
    ]

    identifier = os.urandom(1)[0]

    try:
        packet = _build_radius_packet(COA_REQUEST, identifier, attributes, secret)

        # Add VSAs for bandwidth and data limit
        packet += _build_vsa(WISPR_VENDOR_ID, WISPR_BANDWIDTH_MAX_DOWN, speed_down_kbps)
        packet += _build_vsa(WISPR_VENDOR_ID, WISPR_BANDWIDTH_MAX_UP, speed_up_kbps)
        if data_limit_bytes:
            packet += _build_vsa(MIKROTIK_VENDOR_ID, MIKROTIK_TOTAL_LIMIT, data_limit_bytes)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.sendto(packet, (server, port))

        response = sock.recvfrom(4096)
        sock.close()

        resp_code = response[0][0]
        if resp_code == 44:  # CoA-ACK
            logger.info(f'RADIUS CoA-ACK received for session {session_id}')
            return True
        elif resp_code == 45:  # CoA-NAK
            logger.warning(f'RADIUS CoA-NAK received for session {session_id}')
            return False
        else:
            logger.warning(f'Unexpected RADIUS response code: {resp_code}')
            return False

    except socket.timeout:
        logger.error(f'RADIUS CoA timeout for session {session_id} to {server}:{port}')
        return False
    except Exception as e:
        logger.error(f'RADIUS CoA error: {e}')
        return False


def send_disconnect(server, port, secret, session_id, nas_ip):
    """Send RADIUS Disconnect-Message to terminate a session.

    Args:
        server: RADIUS server IP
        port: CoA port (usually 3799)
        secret: RADIUS shared secret (bytes)
        session_id: RADIUS Acct-Session-Id
        nas_ip: NAS IP address
    """
    attributes = [
        (ATTRIBUTE_ACCT_SESSION_ID, session_id),
        (ATTRIBUTE_NAS_IP_ADDRESS, socket.inet_aton(nas_ip)),
    ]

    identifier = os.urandom(1)[0]

    try:
        packet = _build_radius_packet(DISCONNECT_REQUEST, identifier, attributes, secret)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.sendto(packet, (server, port))

        response = sock.recvfrom(4096)
        sock.close()

        resp_code = response[0][0]
        if resp_code == 41:  # Disconnect-ACK
            logger.info(f'RADIUS Disconnect-ACK for session {session_id}')
            return True
        elif resp_code == 42:  # Disconnect-NAK
            logger.warning(f'RADIUS Disconnect-NAK for session {session_id}')
            return False
        else:
            logger.warning(f'Unexpected RADIUS response code: {resp_code}')
            return False

    except socket.timeout:
        logger.error(f'RADIUS Disconnect timeout for session {session_id}')
        return False
    except Exception as e:
        logger.error(f'RADIUS Disconnect error: {e}')
        return False

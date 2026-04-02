#!/usr/bin/env python3
"""Database seeding script.

Creates sample vouchers and verifies plan data.
Run inside Docker: docker exec faiba-captive-portal python scripts/init_db.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app import create_app
from app.models import db, WiFiPlan, Voucher


def seed_vouchers():
    """Create sample voucher codes for testing."""
    plans = {p.slug: p for p in WiFiPlan.query.all()}

    vouchers = [
        # Hourly vouchers
        ('FAIBA-TEST-0001', 'hourly'),
        ('FAIBA-TEST-0002', 'hourly'),
        # Day pass vouchers
        ('FAIBA-DAY1-2026', 'daily'),
        ('FAIBA-DAY2-2026', 'daily'),
        # Weekly vouchers
        ('FAIBA-WEEK-0001', 'weekly'),
        # Monthly vouchers
        ('FAIBA-MNTH-0001', 'monthly'),
        # Promo codes
        ('WELCOME-FAIBA', 'daily'),
        ('TWORIVERS-VIP', 'weekly'),
        ('GARDENCITY-24', 'daily'),
        ('JTL-STAFF-2026', 'monthly'),
    ]

    created = 0
    for code, plan_slug in vouchers:
        if plan_slug not in plans:
            print(f'  [SKIP] Plan "{plan_slug}" not found for voucher {code}')
            continue
        if Voucher.query.filter_by(code=code).first():
            print(f'  [EXISTS] {code}')
            continue

        v = Voucher(code=code, plan_id=plans[plan_slug].id)
        db.session.add(v)
        created += 1
        print(f'  [CREATED] {code} -> {plan_slug} (KES {plans[plan_slug].price})')

    db.session.commit()
    print(f'\nVouchers: {created} created, {Voucher.query.count()} total')


def show_plans():
    """Display current plans."""
    print('\n--- WiFi Plans ---')
    for p in WiFiPlan.query.order_by(WiFiPlan.price).all():
        status = 'FREE' if p.is_free else f'KES {p.price}'
        print(f'  [{p.id}] {p.name:<15} {status:<10} {p.duration_label:<12} {p.data_mb} MB  {p.speed_down_kbps}kbps down')


if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        print('=== Faiba WiFi Captive Portal - Database Seed ===\n')
        show_plans()
        print('\n--- Seeding Vouchers ---')
        seed_vouchers()
        print('\n=== Done ===')

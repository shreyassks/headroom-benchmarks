"""Seed the tickets SQLite DB with ~2500 realistic synthetic tickets.

Run: `uv run python scratch/langgraph_bench/db/seed.py [--rows 2500]`

Idempotent: drops the table and re-creates from scratch each run.

The point of this DB is not realism per se — it's to give the agent
realistic *shaped* data where Headroom's SmartCrusher has something
to bite into. Bodies are 2-5 sentences of plausible-looking support
prose, varied length (200-900 chars), so compression savings are
visible when many tickets come back as tool results.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from faker import Faker

DB_PATH = Path(__file__).parent / "tickets.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

STATUSES = ["open", "in_progress", "resolved", "closed"]
STATUS_WEIGHTS = [0.30, 0.20, 0.40, 0.10]

CATEGORIES = ["billing", "technical", "account", "shipping", "feature_request", "other"]
CATEGORY_WEIGHTS = [0.25, 0.30, 0.15, 0.15, 0.10, 0.05]

TIERS = ["free", "plus", "premium", "enterprise"]
TIER_WEIGHTS = [0.50, 0.25, 0.18, 0.07]

PRIORITIES = ["low", "medium", "high", "urgent"]
PRIORITY_WEIGHTS = [0.30, 0.40, 0.22, 0.08]

# Per-category templating so the bodies feel coherent. Each tuple is
# (subject_templates, body_templates); the seed picks one of each.
CATEGORY_TEMPLATES: dict[str, tuple[list[str], list[str]]] = {
    "billing": (
        [
            "Charged twice for {plan} subscription",
            "Refund request for order #{order_id}",
            "Invoice {invoice_id} shows incorrect amount",
            "Promo code {promo} didn't apply at checkout",
            "Subscription renewed unexpectedly",
            "Payment method declined repeatedly",
        ],
        [
            "Hi team, I noticed my statement shows two charges of ${amount} on {date} for the same {plan} subscription. "
            "I only signed up once and would like a refund for the duplicate. My account email is {email}. "
            "Please look into this at your earliest convenience.",
            "I'm writing to request a refund for order #{order_id}. The item arrived damaged and the replacement is "
            "backordered indefinitely. I would prefer to return the item and receive a full refund of ${amount}. "
            "I've attached photos of the damage for your reference.",
            "My invoice {invoice_id} shows ${amount} but my contract rate should be ${plan_amount}. Can someone "
            "review and issue a corrected invoice? I have the original agreement on file if needed.",
        ],
    ),
    "technical": (
        [
            "{feature} returns 500 after {date} deploy",
            "API latency spikes every evening around {time}",
            "Cannot authenticate via SSO since {date}",
            "Webhooks for {feature} stopped firing",
            "Mobile app crashes on {platform} {version}",
            "Rate limit headers missing from /v1/{endpoint} responses",
        ],
        [
            "After the deploy on {date} our integration tests started seeing intermittent 500 errors when calling "
            "{feature}. The error rate is roughly 3% and seems to correlate with request payload size. "
            "Logs show stack traces pointing to the {service} handler. Please advise on a workaround while "
            "the team investigates.",
            "We're seeing latency on {feature} climb from ~80ms p50 to ~1.2s p99 between {time_start} and {time_end} "
            "every evening UTC. It's affecting our SLA with downstream consumers. Could someone check the "
            "downstream connection pool sizing? Happy to provide traces from our side.",
            "SSO login has been broken since {date}. Users are getting 'invalid SAML response' errors intermittently. "
            "We've verified our IdP config hasn't changed. This appears to be on your side. {n} users blocked.",
        ],
    ),
    "account": (
        [
            "Cannot change primary email on account",
            "Two-factor reset needed — lost phone",
            "Workspace owner left, need ownership transfer",
            "SSO domain claim rejected — proof document",
            "Account locked after suspicious activity alert",
            "Cannot invite new members to {plan} workspace",
        ],
        [
            "I'm trying to update the primary email on my account from {email_old} to {email_new} but the system "
            "keeps showing 'email already in use'. The new address is brand new. Please help me resolve this so I "
            "can receive notifications at the correct address.",
            "I lost access to the phone number associated with my 2FA. I've verified my identity with my driver's "
            "license and a recent utility bill. Please reset 2FA on my account so I can sign in. Account email: {email}.",
            "Our workspace owner left the company last month and we need to transfer ownership to {new_owner}. "
            "We have admin access and the domain is verified. What is the process to formally transfer ownership?",
        ],
    ),
    "shipping": (
        [
            "Order #{order_id} marked delivered but never arrived",
            "Tracking number shows no movement for {n} days",
            "Wrong address on order — can it be updated?",
            "International customs hold on package",
            "Damaged in transit — replacement needed",
            "Express shipping not honored at checkout",
        ],
        [
            "My order #{order_id} shows 'delivered' on tracking but I have not received it. I checked with "
            "neighbors and the building mailroom. Can you open a lost-package investigation and either redeliver "
            "or refund? Order total was ${amount}.",
            "Tracking for order #{order_id} has not updated in {n} days. The last scan was at {location}. "
            "Is the package lost in transit or just delayed? I'm flexible but need to know — I have a deadline on "
            "{date}.",
        ],
    ),
    "feature_request": (
        [
            "Add bulk export to {feature}",
            "Native dark mode please",
            "Webhook for {event} events",
            "Keyboard shortcut for {action}",
            "Better search filters in {feature}",
            "API endpoint for {feature}",
        ],
        [
            "Our team uses {feature} heavily and we frequently need to bulk export the underlying records to "
            "CSV for offline analysis. Right now we have to script around the per-record API. A native export "
            "would save us hours per week.",
            "Could you add a webhook for {event} events? We have automation that reacts to these and currently "
            "we poll for them every minute, which is wasteful for both sides.",
        ],
    ),
    "other": (
        [
            "General question about {plan} plan limits",
            "How to contact sales for enterprise pricing",
            "Question about data retention policy",
            "Compliance documentation request",
            "GDPR data export request",
        ],
        [
            "I have a question about the {plan} plan limits — specifically the {quota} quota. The docs say "
            "{limit} per month but our dashboard shows we've used {used}. Is there a soft limit or hard limit?",
            "Could you point me to your compliance documentation? We need SOC 2 and ISO 27001 reports for our "
            "vendor security review. Happy to sign an NDA if needed.",
        ],
    ),
}


def make_subject(category: str, fake: Faker) -> str:
    templates, _ = CATEGORY_TEMPLATES[category]
    template = random.choice(templates)
    return template.format_map(_safe_format(fake))


def make_body(category: str, fake: Faker) -> str:
    _, templates = CATEGORY_TEMPLATES[category]
    template = random.choice(templates)
    return template.format_map(_safe_format(fake))


def _safe_format(fake: Faker) -> dict:
    """Build a kwargs dict with every key any template might reference.

    Templates are written loosely — a 'billing' body might mention
    {event} or {location} even though it's primarily a billing template.
    Rather than auditing every cross-reference, we provide a generous
    bag of plausible values; missing keys collapse to ''.
    """
    return {
        "amount":       fake.pydecimal(min_value=20, max_value=999, right_digits=2),
        "plan":         random.choice(TIERS).title(),
        "email":        fake.email(),
        "order_id":     fake.numerify("#####"),
        "invoice_id":   f"INV-{fake.numerify('####')}",
        "promo":        fake.bothify("SAVE##?").upper(),
        "plan_amount":  fake.pydecimal(min_value=10, max_value=499, right_digits=2),
        "date":         fake.date_time_between(start_date="-30d", end_date="now").strftime("%Y-%m-%d"),
        "feature":      fake.word().title() + " API",
        "service":      fake.word() + "_service",
        "endpoint":     random.choice(["users", "events", "search", "exports"]),
        "platform":     random.choice(["iOS", "Android"]),
        "version":      f"{random.randint(15, 18)}.{random.randint(0, 5)}.{random.randint(0, 9)}",
        "time":         fake.time(),
        "time_start":   fake.time(),
        "time_end":     fake.time(),
        "event":        random.choice(["user.created", "billing.charge_failed", "export.completed"]),
        "action":       random.choice(["archive", "duplicate", "share", "export"]),
        "n":            random.randint(2, 14),
        "email_old":    fake.email(),
        "email_new":    fake.email(),
        "new_owner":    fake.name(),
        "location":     fake.city(),
        "quota":        random.choice(["API requests", "storage GB", "team members", "exports"]),
        "limit":        fake.numerify("#,###"),
        "used":         fake.numerify("#,###"),
    }


def make_row(i: int, fake: Faker) -> tuple:
    category = random.choices(CATEGORIES, weights=CATEGORY_WEIGHTS, k=1)[0]
    status = random.choices(STATUSES, weights=STATUS_WEIGHTS, k=1)[0]
    tier = random.choices(TIERS, weights=TIER_WEIGHTS, k=1)[0]
    priority = random.choices(PRIORITIES, weights=PRIORITY_WEIGHTS, k=1)[0]
    customer_id = random.randint(1, 600)

    created = fake.date_time_between(start_date="-90d", end_date="now")
    resolved = (
        created + timedelta(hours=random.randint(1, 240))
        if status in ("resolved", "closed")
        else None
    )

    return (
        i + 1,                                  # id (1-indexed)
        customer_id,
        tier,
        make_subject(category, fake),
        make_body(category, fake),
        category,
        status,
        priority,
        created.isoformat(timespec="seconds"),
        resolved.isoformat(timespec="seconds") if resolved else None,
    )


def seed(n: int, *, seed: int = 42) -> Path:
    random.seed(seed)
    Faker.seed(seed)
    fake = Faker()

    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())

    cols = (
        "id, customer_id, customer_tier, subject, body, category, "
        "status, priority, created_at, resolved_at"
    )
    placeholders = ",".join(["?"] * 10)

    rows = [make_row(i, fake) for i in range(n)]
    conn.executemany(
        f"INSERT INTO tickets ({cols}) VALUES ({placeholders})",
        rows,
    )
    conn.commit()

    # Quick sanity report
    cur = conn.execute("SELECT COUNT(*) FROM tickets")
    total = cur.fetchone()[0]
    cur = conn.execute("SELECT status, COUNT(*) FROM tickets GROUP BY status ORDER BY COUNT(*) DESC")
    by_status = cur.fetchall()
    cur = conn.execute("SELECT category, COUNT(*) FROM tickets GROUP BY category ORDER BY COUNT(*) DESC")
    by_category = cur.fetchall()
    cur = conn.execute("SELECT AVG(LENGTH(body)) FROM tickets")
    avg_body = cur.fetchone()[0]

    conn.close()

    print(f"Seeded {total} rows into {DB_PATH}")
    print(f"  by status  : {dict(by_status)}")
    print(f"  by category: {dict(by_category)}")
    print(f"  avg body   : {avg_body:.0f} chars")
    return DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=2500, help="number of tickets to generate")
    parser.add_argument("--seed", type=int, default=42, help="random seed for reproducibility")
    args = parser.parse_args()
    seed(args.rows, seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

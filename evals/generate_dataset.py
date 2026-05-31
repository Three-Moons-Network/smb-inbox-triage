#!/usr/bin/env python3
"""
Generates evals/golden_dataset.jsonl with 200 samples — 25 per intent across
the 8 intent classes. Run from repo root:

    python3 evals/generate_dataset.py

Why 25 per intent: at the previous size of 2–6 per intent, per-intent accuracy
percentages weren't statistically meaningful (any single misclassification
swung the per-intent percent by 17–50 points). At 25/intent, a single miss
moves the percent by 4 points, so per-intent comparisons become defensible
for routing-by-intent decisions.

Each sample maps directly to the EmailMessage schema in src/classifier/models.py:
    id                  - stable short identifier
    from_address        - sender email
    from_name           - display name (optional)
    subject             - email subject
    body_text           - plain-text body
    expected_intent     - ground-truth intent label
    expected_urgency    - low | medium | high | critical
    expected_sentiment  - positive | neutral | negative

Conventions used here:
  - Real-business-style language with concrete details (order numbers,
    company names, product references) — never lorem ipsum.
  - urgent_escalation samples deliberately include the escalation triggers
    listed in src/classifier/prompts.py (chargeback, review threats, etc.)
    so the prompt updates are evaluable against real text.
  - unknown samples are genuinely ambiguous, not just "short" — short and
    well-formed should still classify if it's clearly e.g. a sales question.
"""
from __future__ import annotations

import json
from pathlib import Path


SAMPLES: list[dict] = []


def add(intent: str, urgency: str, sentiment: str, sender: str, name: str, subject: str, body: str) -> None:
    """Helper to append a sample with auto-assigned id."""
    SAMPLES.append({
        "id": f"e{len(SAMPLES) + 1:03d}",
        "from_address": sender,
        "from_name": name,
        "subject": subject,
        "body_text": body,
        "expected_intent": intent,
        "expected_urgency": urgency,
        "expected_sentiment": sentiment,
    })


# ── 1. sales_inquiry ──────────────────────────────────────────────────────────
# Prospects: pricing / demo / contract / integration / ROI / agency / enterprise
add("sales_inquiry", "medium", "positive",
    "jane@acmecorp.com", "Jane Smith",
    "Pricing for your Enterprise plan",
    "Hi, we're a 50-person team looking for an enterprise solution. Can you send me pricing information and schedule a demo?")
add("sales_inquiry", "medium", "positive",
    "procurement@bluewave.io", "Daniel Park",
    "Annual contract — 250 seats",
    "Our renewal cycle starts in 6 weeks. We currently use a competitor with 250 seats. What's your enterprise tier and is there volume discounting at this scale?")
add("sales_inquiry", "low", "positive",
    "ceo@earlystage.co", "Marisol Vega",
    "Startup discount?",
    "Solo founder here, 3 contractors. Loved the product page. Do you offer a startup tier or any discounting for sub-10-person teams?")
add("sales_inquiry", "medium", "positive",
    "ops@mediumshop.com", "Tom Garrett",
    "Demo request — Tuesday or Wednesday",
    "Can we get a 30-minute demo this week? Tuesday 10–12 PT or Wednesday afternoon both work. Looking specifically at the multi-warehouse feature.")
add("sales_inquiry", "medium", "neutral",
    "rfp@govcontractor.com", "Sarah O'Brien",
    "RFP response — Q3 procurement",
    "We're issuing an RFP for inventory management. Please confirm you can complete a vendor questionnaire and provide SOC 2 documentation. Response deadline 30 days.")
add("sales_inquiry", "medium", "positive",
    "agency@digitalmarketers.net", "Priya Nair",
    "Reseller program",
    "We're a 12-person agency that manages tooling for ~40 SMB clients. Do you have a reseller or agency program with margin or referral fees?")
add("sales_inquiry", "low", "positive",
    "founder@indiehacker.dev", "Marcus Lee",
    "Is there a free tier?",
    "Just stumbled on your product on HN. Is there a free or self-serve tier I can poke at, or is it all sales-led?")
add("sales_inquiry", "medium", "positive",
    "operations@scaleupnow.co", "Hannah Chen",
    "Question about Shopify + NetSuite integration",
    "Quick pre-sales question: does your platform sit between Shopify and NetSuite, or does it replace one of them? We have both and need to pick.")
add("sales_inquiry", "medium", "positive",
    "buyer@retailco.com", "Anthony Russo",
    "Pricing for 10 stores",
    "We operate 10 brick-and-mortar locations plus an online store. Can you walk me through how your multi-location pricing works?")
add("sales_inquiry", "medium", "positive",
    "biztech@manufacturing.us", "Linda Holloway",
    "API access on the standard plan?",
    "Looking at your standard tier — do we get API access at that level, or only on enterprise? We need it for our ERP sync.")
add("sales_inquiry", "low", "positive",
    "student@university.edu", "Aaron Kim",
    "Education pricing",
    "I'm starting a small e-commerce project as part of my MBA capstone. Do you offer education or non-profit pricing?")
add("sales_inquiry", "medium", "positive",
    "cfo@rapidgrowth.io", "Stephanie Wu",
    "ROI calculator and case studies",
    "Trying to build a business case internally. Do you have an ROI calculator or 2–3 case studies from comparable SMBs?")
add("sales_inquiry", "medium", "positive",
    "vc-portfolio@accelpartners.com", "Brendan Carter",
    "Intro from Sequoia portfolio company",
    "Carla at Looplabs (Sequoia portfolio) said your product is what we should look at next. She suggested I reach out to start a conversation about onboarding.")
add("sales_inquiry", "low", "positive",
    "smallbiz@bakery.local", "Frida Lopez",
    "How do you compare to Square?",
    "Hi, I'm running a 2-location bakery. Currently on Square. What would I gain (or lose) by switching to you?")
add("sales_inquiry", "medium", "positive",
    "head@boutiqueconsult.com", "Ravi Singh",
    "Bulk seat license for client deployments",
    "Boutique consulting firm here. We deploy this kind of tool to clients as part of engagements. Do you have a bulk-seat licensing model we can resell?")
add("sales_inquiry", "medium", "positive",
    "ops@ecommscale.com", "Jamie Bell",
    "Migration support from Klaviyo",
    "We're considering moving off Klaviyo. Do you offer migration assistance or services to help us bring across 40K contacts cleanly?")
add("sales_inquiry", "medium", "positive",
    "evaluator@tooltrybuy.com", "Ophelia Voss",
    "Trial extension possible?",
    "I'm in the middle of a trial but my evaluation got bumped a week. Can I get a 7-day extension to run the comparison properly?")
add("sales_inquiry", "low", "positive",
    "growth@d2cbrands.com", "Naomi Ford",
    "Average implementation timeline?",
    "We're trying to plan Q1. From contract signature, what's the realistic median time to live for a 5-person ops team?")
add("sales_inquiry", "medium", "positive",
    "purchasing@hospitalitygroup.co", "Diego Marquez",
    "Multi-property pricing",
    "We own 6 boutique hotels across the southwest. Looking for a unified back-office tool. Can you put together a pricing proposal for 6 properties / ~80 users?")
add("sales_inquiry", "medium", "positive",
    "newdept@megacorp.com", "Lila Adamson",
    "Department-level pilot",
    "Our parent company is on a competitor product but our department of 18 can pilot independently. Can we start with just our team without enterprise procurement?")
add("sales_inquiry", "medium", "positive",
    "tech@partnerco.io", "Vikram Joshi",
    "Embed your widget in our app?",
    "We have ~5K SMB customers and would like to embed your widget in our app under our brand. Is there an OEM / embed program?")
add("sales_inquiry", "medium", "positive",
    "search@familyfirm.com", "Joel Whitman",
    "ETA buyer due diligence — please confirm metrics",
    "I'm an ETA searcher about to acquire a $4M ARR SMB that uses your platform. Can someone share aggregate metrics on retention/upgrades to help underwriting?")
add("sales_inquiry", "low", "positive",
    "ops@nonprofit.org", "Talia Hart",
    "Non-profit pricing inquiry",
    "Registered 501(c)(3) running a regional food bank. Do you have non-profit pricing? Budget is tight but we'd benefit from a real tool.")
add("sales_inquiry", "medium", "positive",
    "evaluator@biztrial.io", "Quinn Aldridge",
    "Security questionnaire — short form",
    "Sales-led trial in progress. Our security team needs you to fill out a short questionnaire (10 questions) before we can connect prod data. Can you turn it around this week?")
add("sales_inquiry", "medium", "positive",
    "growth@dtcfashion.com", "Mei Tanaka",
    "POC scope question",
    "Greenlit a 30-day POC. We want to test scheduling + reporting only. Can the SE limit the demo environment to those two modules?")


# ── 2. support_request ────────────────────────────────────────────────────────
# Existing customer with problems — NO escalation language (no threats,
# no chargebacks, no review threats). Frustration is OK as long as no
# threat is made. Escalation samples live in urgent_escalation.
add("support_request", "medium", "neutral",
    "user@cust1.com", "Alex Cust",
    "Can't log in after password reset",
    "I followed the password reset link this morning, set a new password, but the login page keeps saying 'invalid credentials'. Account email is same as this one.")
add("support_request", "medium", "neutral",
    "ops@retailco.com", "Pat Ngo",
    "Inventory sync failing",
    "Since last Tuesday our Shopify→inventory sync is dropping 5–10% of orders. Job runs but rows are missing. Job ID is sync-2026-05-22-0814.")
add("support_request", "medium", "negative",
    "frustrated.user@email.com", "Riley Mason",
    "Reports not exporting",
    "Trying to export the monthly sales report to CSV and it just spins. Tried Chrome and Firefox. Same result. Could really use this for a board meeting Thursday.")
add("support_request", "high", "negative",
    "ops@warehouse.co", "Mike Sandoval",
    "Mobile app crashing on scan",
    "Whole warehouse team is stuck. The mobile scanner app crashes every time we scan an item starting this morning. We're falling behind on receiving. Help?")
add("support_request", "medium", "neutral",
    "ben@startup.co", "Ben Whittaker",
    "Need to revoke an API key I lost track of",
    "I generated 4 API keys at different times and lost track of which is which. How do I rotate or revoke without breaking the active integration?")
add("support_request", "medium", "neutral",
    "support@cust.com", "Cust Service",
    "Two-factor not sending codes",
    "SMS-based 2FA stopped delivering codes since yesterday. Authenticator app still works for me but my coworker only has SMS set up.")
add("support_request", "medium", "neutral",
    "ana@florist.shop", "Ana Reyes",
    "Subscriber import — duplicate handling",
    "Importing 1,200 subscribers from a Mailchimp export. About 80 already exist in the system. How do I tell the importer to skip dupes vs update them?")
add("support_request", "low", "neutral",
    "miguel@gardencafe.com", "Miguel Torres",
    "How do I change my account email?",
    "I want to change my login email to a new one. I see 'change email' but it asks for a verification code that never arrives.")
add("support_request", "medium", "neutral",
    "operations@studio.com", "Cara Lin",
    "Webhook 500s after upgrade",
    "After last week's upgrade, ~20% of our webhooks are returning 500. URL hasn't changed. Worked fine before the upgrade. Need help diagnosing.")
add("support_request", "medium", "negative",
    "lisa@homegoods.shop", "Lisa Bevan",
    "Discount code not applying at checkout",
    "Customers can apply our discount code but it's not actually taking the percentage off the total. We've lost 3 sales this morning because customers gave up.")
add("support_request", "medium", "neutral",
    "ops@autoparts.biz", "Jordan Vance",
    "User permissions question",
    "I need to give my warehouse manager edit access on inventory but not billing. The role builder is confusing — can someone walk me through it?")
add("support_request", "medium", "neutral",
    "marie@bakery.co", "Marie Dubois",
    "Stripe payouts not showing in dashboard",
    "Stripe shows the payouts but they haven't appeared in our finance dashboard for the last 3 days. Stripe webhook in their dashboard shows success. What's happening on your side?")
add("support_request", "medium", "neutral",
    "tracy@coffeeshop.net", "Tracy Bell",
    "Mobile app stuck on splash screen",
    "iOS app updated to v3.2.1 last night and now just shows the splash screen forever. Other team members on Android are fine.")
add("support_request", "medium", "neutral",
    "finance@cust.com", "Sam Iverson",
    "How do I download a specific month's invoice?",
    "I need April 2026's invoice to attach to an expense report. The invoices page only shows the last 3 months and April isn't there anymore.")
add("support_request", "low", "neutral",
    "hr@biz.com", "Rebecca Kang",
    "Two users seeing different reports",
    "My ops lead and I are looking at the same 'orders this week' report but seeing different totals. Are filters cached per user? How do I align them?")
add("support_request", "medium", "neutral",
    "tech@cust.com", "Owen Hu",
    "SAML SSO assertion errors",
    "Our IT just rolled SAML out to your app. About 30% of users hit 'SAML assertion invalid'. Other apps using the same IdP are fine. Need help comparing assertion configs.")
add("support_request", "medium", "neutral",
    "warehouse@logistics.co", "Carlos Iglesias",
    "Bulk update — UI vs API?",
    "Need to update SKUs on 800 products (price increase). The UI bulk-edit only allows 100 at a time. Is there an API or import-CSV path for this?")
add("support_request", "medium", "neutral",
    "tina@ecom.com", "Tina Wells",
    "Refund processed but customer not notified",
    "Refunded a customer 3 days ago, money went out, but the customer says they never got the refund-confirmation email. Did the email job fail or did I configure something wrong?")
add("support_request", "low", "neutral",
    "ops@artisanal.co", "Helena Brox",
    "Set up auto-replies?",
    "Hoping for a step-by-step on configuring an auto-reply for inbound support tickets after hours. Couldn't find it in the docs.")
add("support_request", "medium", "neutral",
    "newuser@cust.com", "Caroline Pace",
    "Importing CSV fails on column mapping",
    "Trying to import customers. The mapping step keeps saying 'column 'phone' invalid'. Phone column exists and looks fine. Sample row attached.")
add("support_request", "medium", "neutral",
    "user@cust.com", "Mason Reeves",
    "Page load slow today",
    "The dashboard is taking 8–12 seconds to load this morning. Was fast yesterday. Status page is green. Just want to confirm it's known.")
add("support_request", "medium", "neutral",
    "ops@franchise.com", "Yumi Sato",
    "Same email triggering 3 notifications",
    "Every customer signup triggers 3 confirmation emails instead of 1. Started Friday. We didn't change anything on our side. Can you investigate?")
add("support_request", "medium", "neutral",
    "lead@digital.shop", "Aurora Velez",
    "Custom field disappeared from form",
    "We added a 'birthday' custom field two months ago. It's now missing from our intake form. Field still exists in the schema. UI bug?")
add("support_request", "low", "neutral",
    "founder@solofounder.co", "Theo Marin",
    "Where do I find my account ID?",
    "Need my account ID for a third-party integration. I don't see it anywhere obvious in settings. Can you point me at it?")
add("support_request", "medium", "neutral",
    "ops@servicebiz.co", "Diane Halpern",
    "Timezone showing wrong on reports",
    "All our reports show timestamps in UTC even though our org is set to America/Chicago. Already checked the org settings — they're correct.")


# ── 3. billing_question ───────────────────────────────────────────────────────
# Routine invoice/charge questions — NO fraud accusations, NO chargeback
# threats (those live in urgent_escalation).
add("billing_question", "low", "neutral",
    "billing@cust.com", "Pat Cust",
    "Question about last month's invoice",
    "I see two line items for the same plan on my April invoice. Can you confirm whether that's intentional or a duplicate?")
add("billing_question", "low", "neutral",
    "owner@boutique.co", "Sandra Kim",
    "Annual vs monthly pricing",
    "Currently on monthly. Considering switching to annual. Is there a discount and how does the proration work mid-cycle?")
add("billing_question", "low", "neutral",
    "accounts@cust.com", "Wesley Bly",
    "Need W-9 for our vendor records",
    "Our AP team is setting you up as a vendor in our system. Can you send the latest W-9?")
add("billing_question", "low", "neutral",
    "finance@cust.com", "Eric Watanabe",
    "How do I update credit card on file?",
    "Need to swap to a new corporate card. The settings page says 'card on file' but I can't see how to update it.")
add("billing_question", "low", "neutral",
    "ops@shoplocal.co", "Beatriz Caro",
    "Tax not being charged on EU invoices",
    "We have customers in Germany. Our invoices don't show VAT. Is that something I need to enable per-region or do you handle it?")
add("billing_question", "low", "neutral",
    "founder@indie.co", "Tobias Reidel",
    "Pause subscription for 2 months?",
    "Going through a quiet summer. Is there a way to pause my subscription for 8 weeks rather than cancelling and resuming?")
add("billing_question", "low", "neutral",
    "biz@cust.com", "Anais Crowe",
    "Charged on day 1 of trial",
    "Started a 14-day trial yesterday. Already see a charge for the monthly plan. Was the trial canceled or did I miss something?")
add("billing_question", "low", "neutral",
    "ap@retailgroup.com", "Vince Aldama",
    "Bulk invoice export for our auditor",
    "Annual audit. Auditor needs all 12 monthly invoices as PDF in one zip. Can you generate that on the billing portal or do you need to send it?")
add("billing_question", "low", "neutral",
    "user@cust.com", "Kyle Forster",
    "Confused about overage charges",
    "April bill includes a $40 'overage' line. I don't see anywhere in the app that explains what we went over. Can you break it down?")
add("billing_question", "low", "neutral",
    "ops@nonprofit.org", "Lia Petrosky",
    "Apply non-profit discount retroactively?",
    "We just signed up last week and paid first month full price. I see you have non-profit pricing. Can you apply it going forward and refund the difference on this cycle?")
add("billing_question", "low", "neutral",
    "biz@cust.com", "Olin Burch",
    "Downgrade question",
    "We want to go from Growth tier to Starter at the end of the cycle. Does that take effect immediately on the renewal date, or do I lose features now?")
add("billing_question", "low", "neutral",
    "newuser@cust.com", "Iris Quaid",
    "Receipt format for reimbursement",
    "My employer reimburses me but the receipt you send has my personal name on it. Can it be addressed to the company name instead?")
add("billing_question", "low", "neutral",
    "founder@startup.co", "Ezra Holcomb",
    "ACH instead of credit card?",
    "Annual invoice is sizable and credit card fees add up. Can we pay by ACH or wire? What's the process?")
add("billing_question", "low", "neutral",
    "ops@biz.com", "Karina Sosa",
    "Sales tax exemption on file",
    "We're a registered reseller in Texas. How do I upload our resale certificate so future invoices skip sales tax?")
add("billing_question", "low", "neutral",
    "accountspayable@biz.com", "Manolo Pinto",
    "Payment terms — net 30?",
    "Our AP team only pays vendors on net-30. The portal seems to demand payment within 7 days. Can our account be flipped to net-30 billing?")
add("billing_question", "low", "neutral",
    "finance@cust.com", "Ulysses Wynn",
    "Annual renewal price increase",
    "Our renewal quote came back 12% higher than last year. Can you walk me through what changed? I'd like to budget accurately.")
add("billing_question", "low", "neutral",
    "billing@cust.com", "Henrietta Akin",
    "Where to find historical invoices?",
    "I need invoices from 18 months ago for our finance team. Billing portal only shows 12 months. Where do I get older ones?")
add("billing_question", "low", "neutral",
    "ops@cust.com", "Bram Holloway",
    "Add a PO number to invoices",
    "Our procurement requires a PO number on every invoice. Where do I store the PO so it shows up on future invoices?")
add("billing_question", "low", "neutral",
    "founder@cust.com", "Selma Choi",
    "Refund for accidental upgrade",
    "I accidentally clicked the upgrade button last night and got bumped to the next tier (and charged for it). Can you reverse the upgrade and refund?")
add("billing_question", "low", "neutral",
    "accountant@cust.com", "Curtis Rohan",
    "Match invoice line to plan",
    "The invoice says 'Platform Standard — 3 seats' but in the app we have 5 seats configured. Why the mismatch?")
add("billing_question", "low", "neutral",
    "newbiz@cust.com", "Tabitha Vance",
    "Card declined — please retry",
    "Got an email saying my card was declined. I updated the card. Will the system automatically retry or do I need to manually re-trigger?")
add("billing_question", "low", "neutral",
    "founder@bootstrap.co", "Lex Markham",
    "Prorate on plan change",
    "Going from monthly Starter to monthly Growth mid-cycle. Will I be charged the difference now or at next renewal?")
add("billing_question", "low", "neutral",
    "user@cust.com", "Tomas Aleman",
    "Currency on invoices",
    "Our invoices come in USD but we operate in Canada. Can future invoices be in CAD, or do we have to do FX conversion in our books?")
add("billing_question", "low", "neutral",
    "owner@cust.com", "Isadora Penn",
    "Plan includes how many users?",
    "Trying to add a 6th user and the system says my plan only includes 5. Is there a per-seat add-on price or do I need to upgrade the whole plan?")
add("billing_question", "low", "neutral",
    "biz@cust.com", "Felix Coyne",
    "VAT number on EU invoices",
    "Could you add our UK VAT number to the company info so it shows on invoices? Our accountant needs it for the books.")


# ── 4. vendor_outreach ────────────────────────────────────────────────────────
# Suppliers, partners, integrations, SaaS pitches, conferences, beta programs
add("vendor_outreach", "low", "neutral",
    "sales@toolvendor.com", "Marcus Cole",
    "Boost your sales pipeline with our CRM",
    "We help SMBs like yours triple their lead conversion. Quick 15-min demo this week to show you how?")
add("vendor_outreach", "low", "neutral",
    "partnerships@analyticsco.com", "Joanna Wilkes",
    "Integration partnership opportunity",
    "I lead partnerships at AnalyticsCo. Our customers ask for an integration with your platform regularly. Open to a brief call about a joint integration?")
add("vendor_outreach", "low", "neutral",
    "outreach@bizdata.io", "Daria Plamondon",
    "Quick win — automated dashboards for your ops team",
    "Hi! I'm Daria from BizData. We built a Looker-style tool specifically for SMBs. Worth 15 minutes to see if there's a fit?")
add("vendor_outreach", "low", "neutral",
    "sponsorships@summitcorp.com", "Roy Sandler",
    "Sponsor our 2026 SMB Ops Summit",
    "We're organizing the third annual SMB Ops Summit (Sept 2026, 1,200 attendees). Sponsorship packages start at $5K. Interested in receiving the prospectus?")
add("vendor_outreach", "low", "neutral",
    "intro@cloudbackup.net", "Penny Volk",
    "Disaster recovery for SMB SaaS",
    "We provide point-in-time DR snapshots for SMB SaaS platforms. Several of your competitors use us. Open to a chat about how it works?")
add("vendor_outreach", "low", "neutral",
    "biz@procsoftco.com", "Kenji Yamamoto",
    "Procurement software referral program",
    "We run a referral program for SMB-focused SaaS. Earn $200 per qualified referral. Quick call to see if it makes sense for your customer base?")
add("vendor_outreach", "low", "neutral",
    "channel@vendor.io", "Liu Wei",
    "Channel partner program",
    "I run channel partnerships at Vendor. We've signed 30+ SMB-focused tools as fulfillment partners in the past year. Would love to introduce the program.")
add("vendor_outreach", "low", "neutral",
    "growth@adtechco.com", "Sage Bristol",
    "Increase signups 20% with our paid acquisition stack",
    "We've helped 50+ SMB SaaS companies grow paid signups 20%+. Quick discovery call next week?")
add("vendor_outreach", "low", "neutral",
    "events@confgroup.com", "Marta Stelzer",
    "Speaking slot at SaaSCon 2026",
    "I'm curating the SMB track at SaaSCon. Would your CEO be interested in a 30-min talk on multi-cloud ops? October dates.")
add("vendor_outreach", "low", "neutral",
    "supplier@hosting.co", "Brock Hartwell",
    "Cheaper egress for SMB SaaS",
    "We provide bandwidth + egress at 60% of AWS prices for SMB SaaS. Worth 10 minutes to see if there's room to reduce your infra spend?")
add("vendor_outreach", "low", "neutral",
    "leads@securityco.com", "Camille Lacroix",
    "SOC 2 Type II — fast track program",
    "We help SMB SaaS get SOC 2 Type II in under 90 days. Many of your peers have used us. Want details?")
add("vendor_outreach", "low", "neutral",
    "intro@chatbotai.io", "Hiroshi Aoki",
    "AI chatbot for your support team",
    "AI chat agent that resolves 40%+ of L1 tickets autonomously. Several SMB platforms similar to yours are seeing huge deflection numbers. Open to a demo?")
add("vendor_outreach", "low", "neutral",
    "bizdev@fintechco.com", "Marianne Trapp",
    "Embedded payments for your platform",
    "We offer embedded payment processing for SMB platforms. Your customers could accept payments without leaving your app. Conversation?")
add("vendor_outreach", "low", "neutral",
    "outreach@aiwriter.ai", "Soledad Mireles",
    "AI-generated docs for your knowledge base",
    "Hi — we help SMB SaaS auto-generate help-center content from their codebase. Average save: 200 hours of docs writing. Worth a chat?")
add("vendor_outreach", "low", "neutral",
    "alliances@globalcrm.com", "Patrick Donoghue",
    "Strategic alliance — joint go-to-market",
    "Patrick from GlobalCRM here. We've identified an overlap in our SMB customer base. Strategic conversation about co-marketing or technical integration?")
add("vendor_outreach", "low", "neutral",
    "sales@bizinsurance.com", "Vera Klassen",
    "Cyber insurance for SMB SaaS — 12% off",
    "Specialized cyber liability coverage for SMB SaaS. 12% off through end of quarter. Want a quote?")
add("vendor_outreach", "low", "neutral",
    "outreach@reviewco.net", "Phoebe Rosen",
    "Get more G2 reviews",
    "We help SMB SaaS run review-collection campaigns that 5x G2 reviews in 60 days. Worth seeing the process?")
add("vendor_outreach", "low", "neutral",
    "biz@developerco.com", "Jacek Olszewski",
    "Outsource your QA — SE Asia team",
    "We offer dedicated QA pods (4–6 testers) at SE Asia rates. Several SMB SaaS peers use us. Open to a discovery call?")
add("vendor_outreach", "low", "neutral",
    "growth@influenceragency.com", "Bea Calderon",
    "B2B influencer program — SMB founders",
    "We connect SaaS tools with 200+ SMB-founder influencers on LinkedIn. Typical engagement campaign is $12K and generates 80–150 qualified leads. Pitch deck?")
add("vendor_outreach", "low", "neutral",
    "bd@dataco.io", "Soren Ivanchuk",
    "Customer data enrichment",
    "We enrich SMB SaaS customer data (employee count, tech stack, funding) for free-tier segmentation. Want a sample on 100 of your customers?")
add("vendor_outreach", "low", "neutral",
    "intro@managementco.com", "Wendy Kanazawa",
    "Fractional COO for SMB SaaS",
    "Hi! I run a fractional COO firm focused on SMB SaaS at $1–10M ARR. Several of our clients hit profitability inside 6 months. Curious if you're at a relevant stage?")
add("vendor_outreach", "low", "neutral",
    "partners@payrollco.com", "Mac Donaghy",
    "Payroll integration — referral fees",
    "We pay $250 per referred SMB that signs for our payroll service. Want to see the partner program?")
add("vendor_outreach", "low", "neutral",
    "outbound@compliancetool.com", "Greta Eichen",
    "GDPR and CCPA in 30 days",
    "Compliance-as-a-service for SMB SaaS. GDPR + CCPA ready in 30 days. Open to a 15-minute walkthrough?")
add("vendor_outreach", "low", "neutral",
    "sales@dataplatform.io", "Tomas Calvert",
    "BI tool — free for first 10K rows/month",
    "We offer a BI tool free up to 10K rows/month for SMB SaaS. Wanted to put it on your radar — happy to send a 1-pager.")
add("vendor_outreach", "low", "neutral",
    "biz@translationco.com", "Yara Halaby",
    "Translate your product into 30 languages",
    "We translate SMB SaaS interfaces into 30 languages with native QA at a flat rate. Wanted to introduce ourselves in case localization is on your roadmap.")


# ── 5. job_application ────────────────────────────────────────────────────────
add("job_application", "low", "positive",
    "applicant@email.com", "Riley Stoddard",
    "Application for Senior Backend Engineer",
    "Attached is my resume for the Senior Backend role. 8 years of Python / Go, ex-Stripe. Available to start in 4 weeks.")
add("job_application", "low", "positive",
    "jobseeker@gmail.com", "Hannah Bok",
    "Interested in your customer success role",
    "Saw your CS opening on LinkedIn. 6 years at HubSpot and Intercom. Resume attached, happy to schedule a chat.")
add("job_application", "low", "positive",
    "candidate@email.com", "Devon Pierce",
    "Frontend engineer — quick intro",
    "Two-line pitch: ex-Vercel frontend engineer, shipped the design system at my last 3 roles. Open to the React role.")
add("job_application", "low", "positive",
    "hireme@email.com", "Cynthia Vega",
    "Re: Engineering Manager opening",
    "Following up on my application last week for the EM role. Wanted to confirm receipt and ask whether I can expect to hear back in the next two weeks.")
add("job_application", "low", "positive",
    "recruiter@firm.com", "Iliana Yost",
    "Strong product manager candidate for you",
    "I'm a tech recruiter at TalentFirm. I have a senior PM who's currently at Box and wants to move into SMB SaaS. Worth a 15-minute chat to share details?")
add("job_application", "low", "positive",
    "intern@university.edu", "Bao Nguyen",
    "Summer 2026 internship inquiry",
    "Rising senior CS at UCLA. Looking for a backend internship for summer 2026. Resume attached. Would value a chance to contribute to your platform.")
add("job_application", "low", "positive",
    "candidate@email.com", "Reggie Mitsch",
    "Application — DevOps Engineer",
    "Resume + cover letter attached. 5 years AWS + 3 years Kubernetes. Excited about the multi-cloud direction your job post mentioned.")
add("job_application", "low", "positive",
    "applicant@gmail.com", "Ophelia Carstens",
    "QA Engineer — application materials",
    "Hi! Applying for the QA role. 7 years in test automation. Selenium, Playwright, Cypress experience. CV attached.")
add("job_application", "low", "positive",
    "jobs@email.com", "Lucio Pavlik",
    "Product Designer interested",
    "I love what your design team has shipped — clean, opinionated, calm. I'm applying for the Senior PD role. Portfolio link in signature.")
add("job_application", "low", "positive",
    "applicant@email.com", "Maeve Tindall",
    "Salesforce Admin — open to your team",
    "Came across your SFDC admin posting via Hired. 4+ years admin/dev, currently at a 200-person SMB SaaS. Resume attached.")
add("job_application", "low", "positive",
    "recruiter2@firm.com", "Hank Polonsky",
    "Sourcing intro — Engineering candidate",
    "Hi, I'm a sourcer at TopGigs. I have a Bay Area senior engineer with FAANG experience looking for SMB SaaS. Open to a 20-min intro?")
add("job_application", "low", "positive",
    "candidate@email.com", "Renata Schweiger",
    "Following up on Marketing Manager role",
    "Submitted for Marketing Manager 3 weeks ago — wanted to check in politely. Excited about the role, totally understand if timing is off.")
add("job_application", "low", "positive",
    "applicant@email.com", "Thierry Costello",
    "Application — Customer Support Lead",
    "Applying for the Customer Support Lead opening. 9 years CS experience, last 4 leading a 12-person team. CV in PDF attached.")
add("job_application", "low", "positive",
    "jobsearch@email.com", "Daniela Krause",
    "Recent bootcamp grad — junior engineer interest",
    "I'm a recent App Academy graduate (Jan 2026) interested in your junior engineer role. Portfolio + GitHub in signature. Happy to do a take-home.")
add("job_application", "low", "positive",
    "applicant@email.com", "Wally Kimble",
    "Sales — quota-carrying SDR experience",
    "12 quarters carrying a quota at Salesloft. 6 attaining 110%+. Applying for your AE role. CV attached, references on request.")
add("job_application", "low", "positive",
    "writer@email.com", "Inez Ridding",
    "Technical writer application",
    "Tech writer with 7 years SaaS experience. Built knowledge bases at Datadog and Linear. CV + 3 writing samples attached.")
add("job_application", "low", "positive",
    "applicant@email.com", "Salvador Quirke",
    "Open to your DataOps role",
    "Currently at a 500-person SMB SaaS doing DataOps. Snowflake, dbt, Airflow. Looking to move to a smaller team. CV attached.")
add("job_application", "low", "positive",
    "candidate@email.com", "Phoebe Drinkwater",
    "Following up — final round?",
    "I had my fourth round last Thursday for the Senior SRE role. Wanted to politely check in on next steps — I'm holding a competing offer until Friday.")
add("job_application", "low", "positive",
    "recruiter@firm.com", "Vasilios Marek",
    "Engineering candidate — reply when you have time",
    "Tech recruiter representing a strong staff engineer. Background: ex-Stripe, ex-Affirm. Open to a 10-min screening intro?")
add("job_application", "low", "positive",
    "applicant@email.com", "Genevieve Bayard",
    "Application — UX Researcher",
    "Applying for the UX Researcher position. 6 years SaaS, mixed quant/qual. CV + research portfolio attached.")
add("job_application", "low", "positive",
    "newgrad@email.com", "Hassan Ortega",
    "New grad — open to PM associate role",
    "Stanford MBA grad (June 2026). Pre-MBA: 4 years at Square as APM then PM. Applying for the PM Associate posting. CV attached.")
add("job_application", "low", "positive",
    "applicant@email.com", "Lola Castellini",
    "Senior Recruiter — open to your search",
    "Currently a senior recruiter at a Series C SaaS. Closing 4–6 engineers/quarter. Applying for your in-house recruiting lead position.")
add("job_application", "low", "positive",
    "engineer@email.com", "Mateusz Brzozowski",
    "Re: Site Reliability Engineer role",
    "SRE with 10 years at scale (Cloudflare). Comfortable with multi-cloud. Resume attached. Available immediately.")
add("job_application", "low", "positive",
    "applicant@email.com", "Ingrid Vasara",
    "Account executive — quick intro",
    "AE with 5 years SMB-focused SaaS sales (Gusto, Brex). Applying for the AE role posted last week. Resume attached.")
add("job_application", "low", "positive",
    "newbie@email.com", "Cason Pollard",
    "Career-changer — entry support role",
    "Career-changing from 8 years in retail management. Applying for the entry-level support role. Cover letter explains the transition.")


# ── 6. marketing_noise ────────────────────────────────────────────────────────
# Newsletters, drip campaigns, webinar invites, ebook downloads, promo blasts
add("marketing_noise", "low", "neutral",
    "newsletter@marketinghub.com", "Marketing Hub",
    "📈 5 ways to grow your business this quarter",
    "Read our latest blog post on growth strategies. Plus, get our free ebook on customer acquisition. [Unsubscribe]")
add("marketing_noise", "low", "neutral",
    "noreply@webinarinvites.net", "Webinar Invites",
    "[Webinar] How AI is transforming SMB ops",
    "Join us May 30 at 1pm ET for a live webinar. Free registration. Recording available afterward. [Unsubscribe at the bottom of this email]")
add("marketing_noise", "low", "neutral",
    "marketing@b2bblog.io", "B2B Blog",
    "Your weekly B2B insights",
    "This week: 5 SaaS metrics every founder should know, plus a deep dive on PLG vs sales-led. Read more: [link] [Unsubscribe]")
add("marketing_noise", "low", "neutral",
    "promo@tooltrove.com", "Tool Trove",
    "🚀 LIMITED TIME — 40% off all plans",
    "For the next 48 hours only — 40% off any annual plan. Use code SUMMER40 at checkout. Limited time offer. [unsubscribe]")
add("marketing_noise", "low", "neutral",
    "team@digestmag.com", "DigestMag",
    "Daily digest — your industry briefing",
    "Today's headlines: cloud spending up 12% YoY, Apple announces new dev tools, OpenAI ships a new tier. [Read full digest] [Unsubscribe]")
add("marketing_noise", "low", "neutral",
    "ebooks@contenthub.io", "Content Hub",
    "📕 New ebook: The 2026 SMB SaaS playbook",
    "Just published — our 60-page playbook on running an SMB SaaS in 2026. Free download with email signup. [download] [unsubscribe]")
add("marketing_noise", "low", "neutral",
    "events@conf.com", "Conf Events",
    "🎟️ Early bird tickets close Friday",
    "Don't miss SaaSWorld 2026 — early bird tickets ($499) close this Friday. After Friday, $799. [Register]")
add("marketing_noise", "low", "neutral",
    "promo@subboxes.com", "SubBoxes",
    "Your monthly snack box ships tomorrow!",
    "Hi! Your June snack box ships tomorrow morning. Need to skip this month? Click here to skip by 5pm today.")
add("marketing_noise", "low", "neutral",
    "deals@flashsale.net", "Flash Sale",
    "⚡ Flash Sale — ends in 4 hours",
    "Up to 70% off select office gear. Hurry! Ends 5pm Pacific. Shop now.")
add("marketing_noise", "low", "neutral",
    "newsletter@vcnews.io", "VCNews",
    "This week in venture",
    "Top funding rounds, founder interviews, and the VC fund-of-funds debate. Subscribe to read more.")
add("marketing_noise", "low", "neutral",
    "marketing@cloudvendor.com", "Cloud Vendor",
    "Save 30% on cloud infrastructure",
    "Move to our cloud platform and save 30% on infra spend in year 1. Limited-time offer for new accounts.")
add("marketing_noise", "low", "neutral",
    "noreply@blogpost.com", "Blog Post",
    "New: How to scale customer support in 2026",
    "Just published! Our latest guide on scaling support without scaling team. 12-minute read. Comments open.")
add("marketing_noise", "low", "neutral",
    "drip@dripcampaign.io", "Drip Campaign",
    "Quick question, [first_name]",
    "Hey, just checking in — did you see the email I sent on Monday? Wanted to make sure it didn't get buried. [unsubscribe]")
add("marketing_noise", "low", "neutral",
    "noreply@podcast.fm", "Podcast",
    "🎙️ New episode: Building a moat in SMB SaaS",
    "Latest episode is live — 47 minutes with the founder of TopSMB. Listen on Spotify, Apple, or YouTube.")
add("marketing_noise", "low", "neutral",
    "team@coursehub.io", "Course Hub",
    "📚 Master B2B sales — 25% off this week",
    "Our most popular course on B2B sales is 25% off for the next 7 days. 1,200 graduates and counting.")
add("marketing_noise", "low", "neutral",
    "events@summitcorp.com", "Summit Corp",
    "Reminder: SMB Summit 2026 — June 15",
    "Just a reminder that SMB Summit 2026 is two weeks away. Register today to secure your seat. Limited space.")
add("marketing_noise", "low", "neutral",
    "promo@toolfinder.io", "Tool Finder",
    "🛠️ This week's top SMB tools",
    "We rounded up the top 10 SMB tools launching this week. Read the list. [unsubscribe in footer]")
add("marketing_noise", "low", "neutral",
    "newsletter@finbrief.io", "FinBrief",
    "Daily finance brief",
    "Top economic and finance stories of the day. 5-minute read. [subscribe / unsubscribe]")
add("marketing_noise", "low", "neutral",
    "team@bizcourse.com", "BizCourse",
    "🎓 Free workshop — pricing strategy",
    "Free online workshop next Wednesday on pricing strategy. Register today, recording available afterward.")
add("marketing_noise", "low", "neutral",
    "marketing@hostpro.io", "HostPro",
    "🌟 Your hosting renewal is due",
    "Your hosting plan auto-renews in 7 days. Click here to manage your subscription or change plan.")
add("marketing_noise", "low", "neutral",
    "promo@saascontacts.io", "SaaS Contacts",
    "Get 1,000 verified leads — limited offer",
    "1,000 verified B2B leads for $199 (regular $799). 24 hours only. Click to buy.")
add("marketing_noise", "low", "neutral",
    "emails@subwithme.com", "SubWithMe",
    "🎉 You've been subscribed",
    "You've been added to the SubWithMe weekly digest. We send 1 email per week, every Thursday. Welcome!")
add("marketing_noise", "low", "neutral",
    "marketing@adsplatform.io", "Ads Platform",
    "$100 free ad credit for new accounts",
    "Spin up an account and get $100 free to spend on your first campaign. No credit card required.")
add("marketing_noise", "low", "neutral",
    "drip@coldemail.io", "Cold Email",
    "Following up",
    "Hi, just bumping this back to the top of your inbox. Did my last email reach you? Worth 5 minutes? [unsubscribe]")
add("marketing_noise", "low", "neutral",
    "growth@growthtools.io", "Growth Tools",
    "🚀 New product launch — meet Tooly",
    "We just launched Tooly, our new SMB growth platform. Try it free for 30 days. Watch the launch video.")


# ── 7. urgent_escalation ──────────────────────────────────────────────────────
# Coverage across all 7 escalation triggers in the updated prompt
add("urgent_escalation", "critical", "negative",
    "angry.customer@gmail.com", "Bob Jones",
    "Order #12345 STILL not received - this is unacceptable",
    "I placed order 12345 three weeks ago and it still hasn't arrived. I've called twice and nobody helps. If this isn't resolved TODAY I'm disputing the charge and leaving a 1-star review everywhere.")
add("urgent_escalation", "critical", "negative",
    "frustrated@user.com", "frustrated user",
    "Your software is broken AGAIN",
    "This is the third time this month your software has crashed and lost my work. I am paying $200/month for this garbage. Fix it or I'm canceling and going to your competitors. My account is #TRK-8823.")
add("urgent_escalation", "critical", "negative",
    "contact@legalfirm.com", "Johnson & Associates",
    "Notice of potential legal action",
    "This firm represents a client who believes your company has engaged in deceptive business practices. We request a response within 10 business days to avoid formal legal proceedings.")
add("urgent_escalation", "critical", "negative",
    "angry2@example.com", "Upset Customer",
    "Overcharged TWICE this month",
    "I've been charged $99 twice this billing cycle. I want a refund immediately. This is fraud. I'll contact my bank if you don't respond today.")
add("urgent_escalation", "critical", "negative",
    "legal@attorneys-llp.com", "Whittaker Attorneys LLP",
    "Cease and desist demand",
    "We represent Riverstone Inc. Your platform is publishing content that infringes our client's registered trademarks. You have 5 business days to comply or we will file in federal court.")
add("urgent_escalation", "critical", "negative",
    "regulatory@stateagency.gov", "State Consumer Affairs",
    "Consumer complaint — formal response required",
    "A consumer complaint has been filed against your company (case #2026-CA-4421). Under state law you have 14 days to provide a formal response. Failure to do so triggers an investigation.")
add("urgent_escalation", "critical", "negative",
    "media@bigblog.io", "Reporter Sarah Choi",
    "Press inquiry — security incident",
    "I'm a reporter at BigBlog. We have multiple sources reporting customer data exposure on your platform last week. We're publishing Wednesday. Please respond by EOD Monday with comment or we will note 'declined to comment'.")
add("urgent_escalation", "critical", "negative",
    "social@bizinfluencer.com", "Mark Stovall",
    "I'm posting this on Twitter tomorrow",
    "I've been your customer for 2 years. The way your support team treated me yesterday was unacceptable. Unless someone with authority calls me by noon, I'm posting the whole transcript on Twitter — and I have 45K followers.")
add("urgent_escalation", "critical", "negative",
    "biggest@enterprisecust.com", "Catherine Knox",
    "Canceling — effective immediately",
    "After repeated unresolved issues and being ignored on Slack support for 6 days, we are terminating our contract effective immediately. Our legal team will be in touch about prorated refund and data export.")
add("urgent_escalation", "critical", "negative",
    "victim@cust.com", "Erica Hauer",
    "My account was hacked — please act NOW",
    "Someone got into my account at 3am and changed the bank account. Two transfers totaling $4,200 went out. I need this frozen immediately and the transfers reversed. This is a security emergency.")
add("urgent_escalation", "critical", "negative",
    "doctor@hospital.med", "Dr. Yusef Karim",
    "Patient safety issue — your scheduling tool",
    "Your scheduling tool has caused a double-booking that affected a patient appointment yesterday. This is a patient-safety concern. I need confirmation today that the bug is identified and fixed.")
add("urgent_escalation", "critical", "negative",
    "compliance@bigcust.com", "Compliance Officer J. Reyes",
    "SOC 2 violation in your hosted service",
    "Our SOC 2 audit found evidence that your platform is logging credit card data in plaintext. This is an SLA breach under our MSA. We need a remediation plan within 72 hours or we are escalating to our legal team.")
add("urgent_escalation", "critical", "negative",
    "owner@retail.shop", "Maria Costello",
    "Tell my customers something or we lose them",
    "Your outage today has cost us 50+ orders. Customers are calling angry. I will file a chargeback for this month's service AND post about this everywhere. Need a status update within the hour.")
add("urgent_escalation", "critical", "negative",
    "execs@enterprisecust.com", "VP IT Hugh Macomber",
    "SLA breach — invoking termination clause",
    "Per our MSA section 8.3, this is formal notice we are invoking the SLA termination clause. Uptime below 99.5% for two consecutive months. We will be off your platform within 30 days. Prepare for data export.")
add("urgent_escalation", "critical", "negative",
    "review@platinumcust.com", "Stephanie Lloyd",
    "Resolve by 5pm or I dispute with Amex",
    "I've been told three times this would be fixed. It's not. I'm done waiting. Resolve by 5pm today or I dispute this month's $1,800 charge with Amex. This is your last chance.")
add("urgent_escalation", "critical", "negative",
    "executive@biggestcust.com", "CFO Ron Carrington",
    "BBB complaint being filed today",
    "Filing a Better Business Bureau complaint this afternoon. We've been billed $8K incorrectly for two months and your billing team is unresponsive. I want a refund and a written apology by tomorrow EOD.")
add("urgent_escalation", "critical", "negative",
    "safety@constructco.com", "Jenny Tate, EHS",
    "Your equipment data is wrong — safety risk",
    "Your inventory record for our excavator shows last maintenance Feb 2025. Actual maintenance was Feb 2024. We are using this for compliance logs. An OSHA inspection is Friday. We need this corrected immediately.")
add("urgent_escalation", "critical", "negative",
    "compliance@hospitalsys.com", "HIPAA Officer M. Asad",
    "Potential PHI exposure — respond today",
    "We received reports that one of our clinicians' accounts has been displaying another clinic's PHI. This is a potential HIPAA breach. We need confirmation by EOD today and a root-cause analysis within 5 business days.")
add("urgent_escalation", "critical", "negative",
    "leaver@churning.com", "Lucas Holt",
    "Effective immediately — terminating",
    "Two unfixed P0s in a row. We are terminating effective immediately and moving to a competitor. Our finance team will deal with refund and data export questions. Do not contact me on this account again.")
add("urgent_escalation", "critical", "negative",
    "lawyer@consumerrights.org", "Atty. Donna Krieger",
    "Demand letter — refund and damages",
    "This office represents 47 consumers harmed by your platform's bug between March and April. Demand letter attached. Demanding full refunds plus statutory damages. 21 days to respond before we file as a class action.")
add("urgent_escalation", "critical", "negative",
    "founder@biggcust.com", "Liesel Brentwood",
    "Last warning before we leave",
    "We've been patient. Two months of stalling. If our P0 is not resolved by Friday close of business we are terminating our contract and disputing all remaining invoices. We have a 47-page incident log if you want it.")
add("urgent_escalation", "critical", "negative",
    "tweets@founders.club", "James MacAllister",
    "Going public on LinkedIn",
    "Your team has ghosted me for 11 days on a P0 outage. I'm a founder with 30K LinkedIn followers — fellow SaaS founders. I'm writing a full post tomorrow if no one calls me today. This will hurt your funnel.")
add("urgent_escalation", "critical", "negative",
    "ops@majorcust.com", "Hyun-jae Park",
    "Production data corruption — emergency",
    "Your last update corrupted 30% of our production data. We are losing customers in real time. This is a P0. Need an emergency call within the hour or we are filing for breach of contract.")
add("urgent_escalation", "critical", "negative",
    "complaint@cust.com", "Anwar Nasri",
    "Filing chargeback this afternoon",
    "I've been overcharged for 4 months. Each time your team promises to fix it and never does. I'm filing a chargeback with Chase for the cumulative $1,600 this afternoon unless you can prove the refund is processing within 2 hours.")
add("urgent_escalation", "critical", "negative",
    "former@cust.com", "Pia Sönnerstedt",
    "Public review going up Monday",
    "I've documented every interaction over 90 days. Your team has been dismissive, your product has cost us 3 customers, and your CEO refuses to call back. I'm publishing a detailed Trustpilot, Capterra, and G2 review on Monday. Last chance to address before then.")


# ── 8. unknown ────────────────────────────────────────────────────────────────
# Genuinely ambiguous — short fragments, bounce notifications, auto-replies,
# off-topic messages, AI-generated spam, gibberish
add("unknown", "low", "neutral",
    "noreply@mailerdaemon.com", "Mail Delivery System",
    "Undeliverable mail",
    "Your email could not be delivered. SMTP error 550: mailbox unavailable. Return-path: <postmaster@example.com>.")
add("unknown", "low", "neutral",
    "out-of-office@cust.com", "Cust",
    "Auto: Out of office until June 15",
    "I am out of the office returning June 15. For urgent matters, contact my assistant at assistant@cust.com. This is an automated reply.")
add("unknown", "low", "neutral",
    "test@anonymous.com", "Test",
    "test",
    "test")
add("unknown", "low", "neutral",
    "??@??.com", "??",
    "????",
    "?")
add("unknown", "low", "neutral",
    "founder@cust.com", "Random Person",
    "FYI",
    "FYI")
add("unknown", "low", "neutral",
    "user@cust.com", "Confused User",
    "?",
    "I don't know what this is for. Sorry. Maybe this email got sent to the wrong place. Disregard.")
add("unknown", "low", "neutral",
    "ai@spambot.io", "AI Sender",
    "Increase your revenue dear sir/madam",
    "Dear sir/madam, we provide all the things business needs. Please reply to discuss your project. Many thanks.")
add("unknown", "low", "neutral",
    "noreply@calendar.app", "Calendar",
    "Event reminder: Lunch",
    "Lunch — Today at 12:30pm — Office Cafe. Reminder set 30 minutes before.")
add("unknown", "low", "neutral",
    "noreply@security.app", "Security",
    "Your password was used",
    "We noticed your password was used to sign in to a service from Chrome on macOS at 14:22 UTC. If this was you, no action is needed.")
add("unknown", "low", "neutral",
    "spammer@randomdomain.xyz", "Spammer",
    "URGENT URGENT URGENT URGENT URGENT",
    "Hi i think you have what i want pls call me")
add("unknown", "low", "neutral",
    "no-reply@github.com", "GitHub",
    "[acme/repo] PR opened: Fix typo in README",
    "User jdoe opened pull request #142 against main: Fix typo in README. Reply to merge or comment.")
add("unknown", "low", "neutral",
    "unknownsender@cust.com", "Unknown",
    "Hi",
    "Hi")
add("unknown", "low", "neutral",
    "stranger@email.com", "Unknown Stranger",
    "Where are you?",
    "Where are you? Has it been long?")
add("unknown", "low", "neutral",
    "noreply@subscriptions.app", "Subscriptions",
    "Reminder: subscription renews soon",
    "This is a system notification about your Spotify Family subscription renewing next week. No action required.")
add("unknown", "low", "neutral",
    "scammer@scammer.xyz", "Prince of Nowhere",
    "Confidential business proposal",
    "Dear esteemed beneficiary, I am a prince from somewhere and I have $25 million USD to transfer to your account. Please reply with your bank details.")
add("unknown", "low", "neutral",
    "writer@theatre.org", "Local Theatre",
    "We need actors for our community play",
    "Our community theatre is looking for volunteer actors for our June production of Hamlet. No experience required.")
add("unknown", "low", "neutral",
    "neighbor@email.com", "Neighbor",
    "lost cat",
    "Have you seen my cat? Tabby, missing since Tuesday. Comes when called for Pickles.")
add("unknown", "low", "neutral",
    "noreply@flightupdate.com", "Flight Update",
    "Your flight UA-2245 has been delayed",
    "Your flight UA-2245 from SFO to JFK on June 3 has been delayed 45 minutes. New departure: 16:25 PDT.")
add("unknown", "low", "neutral",
    "wrongperson@email.com", "Mistaken Sender",
    "Wedding photos!",
    "Hi mom! Attaching the photos from cousin Sarah's wedding. Tell dad I said hi! Love you!")
add("unknown", "low", "neutral",
    "questionmark@cust.com", "Confused Person",
    "I don't think this is right",
    "Something doesn't seem right but I can't tell what. Maybe nothing. Please ignore.")
add("unknown", "low", "neutral",
    "broken@bot.io", "Broken Bot",
    "{{first_name}}",
    "Hi {{first_name}}, hope all is well at {{company_name}}. {{personalized_intro}}.")
add("unknown", "low", "neutral",
    "noreply@ticketing.app", "Ticketing System",
    "Your support ticket is closed",
    "Support ticket #SR-99812 has been marked closed. If you need to reopen, reply to this email.")
add("unknown", "low", "neutral",
    "automatic@cust.com", "Automatic Reply",
    "Re: your inquiry",
    "Thank you for contacting our team. We will respond within 2 business days. — Automated response, do not reply.")
add("unknown", "low", "neutral",
    "wronglist@distro.com", "List Admin",
    "All-hands meeting tomorrow",
    "Reminder to the entire ACME engineering organization — tomorrow's all-hands is at 10am Pacific in the SF office. — Sent to engineering@acme.com.")
add("unknown", "low", "neutral",
    "anon@anonymous.org", "Anonymous",
    "I have something to tell you",
    "I have something I want to share with you but I'm not sure how. Maybe I'll figure it out. Bye for now.")


# ── Write the JSONL file ─────────────────────────────────────────────────────
def main() -> None:
    out = Path(__file__).parent / "golden_dataset.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for sample in SAMPLES:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    # Per-intent count summary
    counts: dict[str, int] = {}
    for s in SAMPLES:
        counts[s["expected_intent"]] = counts.get(s["expected_intent"], 0) + 1

    print(f"Wrote {len(SAMPLES)} samples to {out}")
    print("Per-intent breakdown:")
    for intent in sorted(counts):
        print(f"  {intent:20s} {counts[intent]:>3d}")


if __name__ == "__main__":
    main()

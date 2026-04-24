# Test Fixtures

## Overview

This fixture pack provides realistic test data for the open-brain ingest pipeline.
It covers three input types:

- **macwhisper/**: MacWhisper-formatted meeting transcripts and dictated notes
- **email/**: RFC 822 `.eml` files covering different email patterns (threads, newsletters, introductions)
- **people/**: JSON person records for deduplication and identity-resolution tests

All files use fictional names except where noted as `polaris_confirmed` consent subset.
No real personal data is present except for Polaris network members who have given consent.

---

## macwhisper/

MacWhisper transcript format: timestamped lines like `[HH:MM:SS] Speaker Name: [speech]`.

### sample_transcript_3person_meeting.txt

- **Content:** German/English code-switched business meeting discussing a software integration project (API integration, data mapping, SSL certificates, next steps)
- **Language:** German with English technical terms mixed in
- **Speakers (present):** Sarah Hoffmann (Produktmanagerin), Marcus Berger (Technik), Priya Nair (Cognovis)
- **Mentioned (absent):**
  - Dr. Cyrus Alamouti (Polaris, consent-confirmed) — wants a demo of the Appointment Sync feature
  - Tobias Schreiber (fictional) — sick, absent; owns backend Auth Flow work
  - Lisa Chen (fictional, Cognovis) — reviewed data mapping, has comments on Budgetplanung fields
  - Jan Kowalski (fictional) — Rechenzentrum, responsible for SSL certificates (name uncertain)
- **Duration:** ~5 minutes simulated (timestamps 00:00–00:05:24)
- **Word count:** ~750 words
- **Expected extraction hints:**
  - `meeting_type`: in_person/video, 3 active speakers
  - `attendees`: Sarah Hoffmann, Marcus Berger, Priya Nair
  - `mentioned_people`: 4 (Cyrus Alamouti, Tobias Schreiber, Lisa Chen, Jan Kowalski)
  - Canonical form for mentioned person: `"Dr. Cyrus Alamouti"` with alternates `["Cyrus", "Dr. Alamouti"]`
  - `action_items`: at least 3 (Marcus finishes Error Handling, Priya sends Staging Credentials + Webhook docs, Sarah arranges Lisa meeting + SSL follow-up + PDF spec)
  - `language`: de/en mixed

### sample_transcript_dictated_note.txt

- **Content:** Single speaker dictating follow-up notes (likely while driving/walking) — references a call with David Park, a concern from Emma Torres, and a vendor comparison from Robert Whitfield
- **Language:** English
- **Speakers:** 1 (labeled "Speaker", first-person dictation)
- **Mentioned people:**
  - David Park (fictional) — on board with revised timeline; needs contract by Thursday
  - Emma Torres (fictional) — concerns about data migration scope for historical records
  - Robert Whitfield (fictional) — sent vendor comparison sheet; pricing delta smaller than expected
- **Duration:** ~90 seconds simulated (timestamps 00:00–00:01:22)
- **Word count:** ~150 words
- **Expected extraction hints:**
  - `note_type`: dictated_followup
  - `mentioned_people`: 3 (David Park, Emma Torres, Robert Whitfield)
  - Dense name references — all 3 people mentioned multiple times in short span
  - `action_items`: 3 (send contract to David, set up 3-way call, review vendor sheet)
  - `language`: en

### sample_transcript_long_meeting.txt

- **Content:** Quarterly review meeting that starts with Q1 numbers, drifts into Q2 roadmap discussion, then into an unplanned team restructuring conversation. Includes natural interruptions, topic changes, and off-agenda items.
- **Language:** German with English business terms (roadmap, pipeline, RBAC, squad, etc.)
- **Speakers (present):** Katharina Meier, Stefan Wolf, Annika Baum, Michael Torres
- **Mentioned (absent):**
  - Jochen Jungbluth (Polaris, consent-confirmed) — called Stefan about Q2 guidance
  - Implicit: Engineering team members not named (no other non-attendee names appear in this transcript)
- **Duration:** ~12 minutes simulated (timestamps 00:00–00:11:50)
- **Word count:** ~1500 words
- **Agenda drift pattern:**
  1. Q1 Revenue numbers
  2. R&D over-budget question (mid-agenda interruption)
  3. Product metrics and retention
  4. Q2 roadmap preview (unplanned topic extension)
  5. Engineering capacity / resource constraints
  6. Unplanned team restructuring discussion (sensitive, Katharina deflects)
  7. Customer Success staffing issue (late addition)
- **Expected extraction hints:**
  - `meeting_type`: quarterly_review with agenda drift
  - `attendees`: 4 speakers
  - `mentioned_people`: Jochen Jungbluth (+ implicit others)
  - `topics`: Q1 financials, product metrics, Q2 roadmap, RBAC, org restructuring, CS hiring
  - `language`: de/en mixed
  - Suitable for chunking tests — transcript is long enough to require splitting

---

## email/

All `.eml` files use RFC 822 format with proper headers (Message-ID, Date, MIME-Version, Content-Type).

### reply_thread.eml

- **Content:** 3-message thread about scheduling a technical meeting on data integration. Initial cold outreach → reply agreeing to meet → confirmation with agenda.
- **Format:** Single `.eml` file; older messages embedded as quoted text; thread structure indicated via `In-Reply-To` and `References` headers
- **People:**
  - Thomas Bauer (fictional, sender) — `thomas.bauer@techpartner.example`, Solution Architect at TechPartner Solutions
  - Anna Weber (fictional, recipient) — `anna.weber@example-dental.com`, IT-Koordination
- **Language:** German
- **Expected extraction hints:**
  - `email_type`: reply_thread, scheduling
  - `participants`: Thomas Bauer, Anna Weber
  - `action_items`: meeting confirmed for Thu 16 Apr 2026 10:00, Thomas to send calendar invite
  - `personal_interaction`: True (direct scheduling between two named individuals)
  - Should NOT be filtered out — genuine person-to-person interaction

### newsletter.eml

- **Content:** Monthly HTML newsletter from "FHIR Deutschland" mailing list — links to implementation guides, upcoming events (Connectathon, webinars, workshops), community tools
- **Format:** `text/html` (single-part), with proper newsletter headers
- **People:** None — no personal names, no personal interaction
- **Sender:** `FHIR Deutschland <newsletter@fhir-community.example>` (mailing list)
- **Recipient:** `Malte Sussdorff <malte@personal.example>` — **This is the fixture owner's own identity, not a third party.** Malte Sussdorff is the author of this fixture pack; his name and email alias appear here as a self-consented, consent-confirmed fixture subject (same Polaris consent scope). No third-party personal data is present.
- **Language:** German
- **Special headers:** `List-Unsubscribe`, `List-ID`, `Precedence: bulk`, `X-Mailer: MailChimp`
- **Expected extraction hints:**
  - `email_type`: newsletter / mailing_list
  - `personal_interaction`: False
  - **FILTER: This email should be excluded from ingest** — no sender/recipient personal relationship, no named individuals, bulk mailing list traffic
  - `filter_reason`: newsletter, no_personal_interaction, Precedence=bulk

### forwarded_intro.eml

- **Content:** Max Richter introduces Dr. Lena Hoffmann to Oliver Schneider via email. Max explains both parties' backgrounds and why they should connect. Includes a forwarded original message from Oliver that sparked the introduction.
- **Format:** Plain text with forwarded message embedded; `To:` has two recipients (CC-style intro)
- **People:**
  - Max Richter (fictional, sender) — `max.richter@consultant.example`, freelance Digital Health consultant
  - Dr. Lena Hoffmann (fictional, introduced) — `lena.hoffmann@clinic.example`, Fachärztin, software implementation lead, FHIR Community member
  - Oliver Schneider (fictional, recipient) — `oliver.schneider@practice.example`, Gemeinschaftspraxis Köln
- **Language:** German
- **Expected extraction hints:**
  - `email_type`: introduction / forwarded
  - `participants`: Max Richter (sender), Dr. Lena Hoffmann (introduced), Oliver Schneider (recipient)
  - `relationship_signal`: Max Richter introduces Dr. Hoffmann to Oliver Schneider
  - `personal_interaction`: True
  - Should result in person records for all three individuals and a relationship edge: Max→Lena (knows), Max→Oliver (knows), introduction of Lena↔Oliver

---

## people/

Static JSON fixture files with structured person records. Used for deduplication, identity resolution, and ingest schema tests.

Note: `directory_polaris.json` and `synthetic_testperson.json` use a flat `organization` string field; `singleton_weihe.json` uses an `organizations` array of objects with `name`+`description`. This divergence is intentional — it exercises schema flexibility in dedup/extraction tests.

### directory_polaris.json

- **Content:** JSON array with 5 members of the Polaris network (consent-confirmed subset from memory 18175)
- **Schema:** Array of objects with keys: `canonical_name`, `known_as`, `role`, `organization`, `linkedin_url`, `notes`, `consent_status`
- **People:**
  - Elias Trewin — Cinify, data warehousing for dental workflow KPIs
  - Dr. Cyrus Alamouti — Dental-Now co-principal, Zahnarzt; appears in `sample_transcript_3person_meeting.txt`
  - Siamak Ghasemi — Dental-Now co-principal, enterprise IT background
  - Jochen Jungbluth — Dental-Now corporate/tax counsel, ex-KPMG; appears in `sample_transcript_long_meeting.txt`
  - Philipp Kuhn-Regnier — Sonia (sonia.so), ex-CFO Zahn 1
- **Expected extraction hints:**
  - All records have `consent_status: "polaris_confirmed"`
  - `known_as` arrays should drive alternate-name matching in dedup logic
  - Cross-reference: Cyrus Alamouti and Jochen Jungbluth appear in transcript fixtures — dedup tests should match these records

### singleton_weihe.json

- **Content:** Single JSON object for Dr. Dr. Stephan Weihe (consent-confirmed, memory 17692)
- **Schema:** Object with keys: `canonical_name`, `known_as`, `role`, `organizations` (array of objects with `name` and `description`), `location`, `notes`, `consent_status`
- **Notable:** Multiple past and present organizations — tests dedup logic for complex multi-org affiliations
- **Expected extraction hints:**
  - Double-title handling: `"Dr. Dr."` prefix should be preserved in canonical form
  - `known_as` alternates: `["Stephan Weihe", "Dr. Weihe"]` — without double title
  - `organizations`: 6 entries (3 active/current, 3 previous)
  - `consent_status: "polaris_confirmed"`

### synthetic_testperson.json

- **Content:** Fully synthetic person record with no real-world identity
- **Schema:** Object with keys: `canonical_name`, `known_as`, `role`, `organization`, `email`, `phone`, `linkedin_url`, `tags`, `notes`, `consent_status`
- **Purpose:** Clean baseline for ingest/dedup pipeline tests — use to verify extraction schema without any risk of real-data contamination
- **Expected extraction hints:**
  - `consent_status: "synthetic"` — ingest logic should treat this as safe for any test
  - `linkedin_url: null` — tests nullable field handling
  - `tags: ["test", "synthetic", "baseline"]` — fixture tagging
  - Can be freely modified in tests without consent concerns

# Creator Outreach Tracker

A local-first command-line database for tracking outreach to online content creators and guest-post opportunities.

JSON is the source of truth. CSV import and export are supported.

## Funnel stages

- punch1_sent
- punch1_reprompt_sent
- punch1_reply_received
- advice_implemented
- punch2_sent
- punch2_reply_received
- guest_post_permission_granted
- guest_post_declined
- guest_post_submitted
- guest_post_published
- closed_no_response

## Add a creator

```bash
python3 creator_outreach_tracker.py add --db creator_outreach_master.json

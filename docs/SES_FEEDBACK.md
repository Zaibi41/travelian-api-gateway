# SES bounces and complaints (gateway#59)

The gateway accepts SES bounce and complaint notifications from Amazon SNS at `POST /v1/webhooks/ses`. Each message must
be signed by SNS (the certificate comes from `https://sns.<region>.amazonaws.com/...`) and come from a topic listed in
`SES_SNS_TOPIC_ARNS`. While that setting is empty, every message is refused (403), so nothing happens until the steps
below are done.

## What staff see
Admin → booking → Overview → **Email not sent**:
- **Bounced** (permanent) or **marked as spam**: "Contact the customer and fix the email address.", with a field to
  enter the corrected address. Until it's changed, no more emails go to the bad address.
- **Bounced for now** (mailbox full and similar): Resend later.
- After the address is changed: "Resend this email to the new address", with **Resend email**.

## One-time AWS setup (owner, us-east-1, where the gateway sends from)
1. **SNS → Topics → Create topic**: Standard, name `travelian-ses-feedback`. Copy its ARN.
2. **SES → Identities → travelian.pk → Notifications → Edit**: Bounce feedback and Complaint feedback → that topic.
   Leave "Include original headers" off.
3. On the server, add to `.env`: `SES_SNS_TOPIC_ARNS=<the topic ARN>` and restart the gateway.
4. **SNS → the topic → Create subscription**: Protocol HTTPS, endpoint `https://api.travelian.pk/v1/webhooks/ses`.
   The gateway confirms it by itself (the subscription shows "Confirmed" within a minute).
5. Optional, once confirmed: SES → travelian.pk → Notifications → turn **Email feedback forwarding** off.

## Test
Send a booking email to `bounce@simulator.amazonses.com` and one to `complaint@simulator.amazonses.com`. The simulator
addresses work while SES is still limited. Within a minute the booking shows the flag. The server log has
"Customer email bounced" with the reference only (no addresses).

# Website flight payments: go-live checklist

Covers travelian (website + admin) and travelian-api-gateway, branch `feat/flight-payments` in both. The same file is in
both repos' `docs/`. Nothing here was run against AWS or the server; every command is for you to run.

## 1. Before deploying

- [ ] **PayFast production credentials.** The gateway is on UAT (`PAYFAST_BASE_URL=https://ipguat.apps.net.pk`). Put the
      production `PAYFAST_MERCHANT_ID` and `PAYFAST_SECURED_KEY` in Parameter Store (as `deploy/aws-setup.sh` does for UAT)
      and set `PAYFAST_BASE_URL` to the production host PayFast gives you. Production PayFast must allow
      `CHECKOUT_URL=https://api.travelian.pk/v1/public/payments/payfast/notify`.
- [ ] **Southair funded account.** UAT has a zero balance, so automatic issuing is refused ("Not enough supplier balance")
      and every card booking becomes a red alert. Production credentials and a funded balance are needed before
      `SOUTHAIR_TICKETING_ENABLED=true` means anything.
- [ ] **SooperFare issuing is not built.** `/v1/booking/confirm` has no documented answer (no example response;
      `txn_ref_id` only "TEST"). Card works for SooperFare fares, but staff issue in SooperFare's portal after the red alert.
      Ask SooperFare for the confirm response format and what `txn_ref_id` must be.
- [ ] **Database backup:** `pg_dump -h 127.0.0.1 -U travelian_app travelian > ~/travelian-$(date +%F-%H%M).sql`

## 2. Deploy order

1. Gateway: deploy the release with every flag below still **off** (`deploy/deploy.sh`).
2. Gateway: `bash /opt/travelian-api/current/deploy/migrate.sh`, then `… migrate.sh current` must show
   `0025_paid_not_ticketed`. (0025 adds the 'paid_not_ticketed' booking status. `migrate.sh` runs the active release's
   migrations, so it comes after step 1; until it runs, a paid hold that lapses can't be marked and the expiry job logs it.)
3. Website: build and deploy (the admin needs the new statuses, filter, card and the Edit account dialog).
4. Turn flags on (section 3), restart the gateway, and run one UAT card booking end to end before real customers.

## 3. Gateway `.env` / Parameter Store

| Setting | Go-live value | Notes |
|---|---|---|
| `PUBLIC_FLIGHT_BOOKING_ENABLED` | `true` | Off: website flights stay requests (no hold, no payment). |
| `SOUTHAIR_BOOKING_ENABLED` / `SOOPERFARE_BOOKING_ENABLED` | `true` per supplier | Needed for the website to hold a fare. |
| `SOUTHAIR_TICKETING_ENABLED` | `true` only with a funded production account | Off: every card payment goes to staff (red alert). |
| `SOOPERFARE_TICKETING_ENABLED` | leave `false` | Not used yet (issuing not built). |
| `BANK_TRANSFER_ENABLED` | `true` | And at least one active bank account with title + number/IBAN/QR in Accounts & Ledgers. |
| `JAZZCASH_ENABLED` | `false` | JazzCash is a scaffold; leave off until its flow is confirmed. |
| `FLIGHT_ISSUANCE_STUCK_MINUTES` | `10` (default) | Stuck-issuance recovery: one retry after 10 minutes. |
| `NOTIFY_EMAIL_ENABLED` | `true` once SES works (section 5) | Off: alerts only in the admin. |
| `NOTIFY_EMAILS` | staff addresses, comma-separated | |
| `NOTIFY_EMAIL_FROM` | `Travelian <alerts@travelian.pk>` (or `noreply@`) | Must be on the SES-verified domain. |
| `SES_REGION` | the region where travelian.pk is verified (default `us-east-1`) | |
| `ASSETS_BUCKET` | `travelian-assets-<account id>` | From `deploy/aws-setup.sh`. |
| `ASSETS_REGION` | the bucket's region (e.g. `ap-southeast-1`) | Uploads are signed for this region (SigV4). |
| `ASSETS_PUBLIC_BASE_URL` | `https://<bucket>.s3.<region>.amazonaws.com` | Images are read from here: needs public read (section 4). |
| `PUBLIC_SITE_URL` / `PUBLIC_API_URL` | `https://travelian.pk` / `https://api.travelian.pk` | PayFast return and notification URLs. |

## 4. S3: uploads and public read

Uploads (site logo `site/`, payment QR `payment-qr/`, payment proofs `payment-proof/`) go straight from the browser to S3
with a SigV4 signed POST. Logos and QR images are shown as plain `<img src="ASSETS_PUBLIC_BASE_URL/…">` on the website and
at checkout, so those two folders need public read. Recommended: **a bucket policy for public `GetObject` on `site/*`
and `payment-qr/*` only** (keys are random UUIDs, listing stays private, `payment-proof/` stays private). Signed GET URLs
would expire inside cached pages and the gateway would have to re-sign on every page load: not worth it for public images.

Check (read-only):

```bash
BUCKET=travelian-assets-<account id>
aws s3api get-public-access-block --bucket "$BUCKET"   # BlockPublicPolicy and RestrictPublicBuckets must be false
aws s3api get-bucket-policy --bucket "$BUCKET"
aws s3api get-bucket-cors --bucket "$BUCKET"           # POST from https://travelian.pk and https://www.travelian.pk
curl -I "https://$BUCKET.s3.<region>.amazonaws.com/payment-qr/<an uploaded key>"   # expect 200
```

Apply if missing (the same as `deploy/aws-setup.sh`):

```bash
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false
aws s3api put-bucket-policy --bucket "$BUCKET" --policy '{"Version":"2012-10-17","Statement":[{"Sid":"PublicReadImages",
  "Effect":"Allow","Principal":"*","Action":"s3:GetObject",
  "Resource":["arn:aws:s3:::'"$BUCKET"'/site/*","arn:aws:s3:::'"$BUCKET"'/payment-qr/*"]}]}'
aws s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration '{"CORSRules":[{"AllowedOrigins":["https://travelian.pk",
  "https://www.travelian.pk","http://localhost:8080"],"AllowedMethods":["POST"],"AllowedHeaders":["*"],"MaxAgeSeconds":3000}]}'
```

If the account-level Block Public Access is on (`aws s3control get-public-access-block --account-id <id>`), the bucket
policy is ignored until `BlockPublicPolicy`/`RestrictPublicBuckets` are off there too. The server IAM user needs
`s3:PutObject` on the three folders (granted by `aws-setup.sh`).

## 5. SES (booking alert email)

1. In SES (`SES_REGION`), verify the **domain** `travelian.pk` (Easy DKIM: add the three CNAME records SES shows to the
   DNS). Domain verification lets any `@travelian.pk` sender work; the server IAM policy already allows only
   `*@travelian.pk` senders (`aws-setup.sh`).
2. Optional but recommended: a custom MAIL FROM (e.g. `mail.travelian.pk`) and a DMARC record, so alerts don't go to spam.
3. **Sandbox:** new SES accounts are in the sandbox and can only send to verified addresses (200/day). Either verify each
   staff address in `NOTIFY_EMAILS` (fine for a few staff), or request production access (SES console > Account
   dashboard > Request production access; use case: transactional staff alerts). Check:
   `aws sesv2 get-account --region <SES_REGION>` (`ProductionAccessEnabled`).
4. Set `NOTIFY_EMAIL_ENABLED=true`, `NOTIFY_EMAILS=…`, `NOTIFY_EMAIL_FROM=Travelian <alerts@travelian.pk>`,
   `SES_REGION=…`, restart the gateway. The admin's Notifications page stops showing "Email alerts aren't set up yet".
5. Until then the gateway logs every skipped alert email, e.g. `Red alert email not sent (skipped: NOTIFY_EMAIL_ENABLED is
   off)` or `(refused by SES (MessageRejected: the sender address or domain isn't verified yet))`.

## 6. While email is off: what staff watch

- **Admin > Notifications > Booking alerts** (bell in the user menu): red "Urgent" rows mean a customer paid by card
  and has no ticket. Act on each: confirm with the supplier, issue, share the ticket with the customer.
- **Admin > Bookings > Flights**: the red "Paid, not ticketed" card and the status filter values "Paid, issuance pending"
  (hold still valid: issue now) and "Paid, not ticketed: refund or rebook" (hold lost: refund or rebook, contact the
  customer). Nothing is refunded automatically.
- A second urgent alert arrives when about 30 minutes of hold are left. Bank-transfer and JazzCash holds arrive as normal
  "Held for bank transfer" alerts: record the money in the Payments tab, then issue.
- Check the Notifications page at least every 30 minutes during sales hours (the page refreshes every minute).

## 7. After go-live

- [ ] One real card booking per supplier; check: payment recorded once, red alert or ticket, admin statuses, customer
      message on My Bookings.
- [ ] Lightsail snapshots and alarms (roadmap open item) before real customer data accumulates.

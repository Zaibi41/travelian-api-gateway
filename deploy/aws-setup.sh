#!/usr/bin/env bash
# One-time AWS setup for the gateway (run in AWS CloudShell as an administrator). Safe to read before running.
# Creates: the S3 bucket for admin uploads (D38), a least-privilege IAM user for the Lightsail server (S3 uploads,
# SES staff email, Parameter Store reads under /travelian/gateway), and the Parameter Store entries (D, D32).
# It prints the server settings to paste into /opt/travelian-api/shared/.env. Nothing secret is printed except the new
# access key, which is shown once: paste it straight into the server's settings file, never into chat or the repo.
set -euo pipefail

ACCOUNT=106237071463
BUCKET=travelian-assets-$ACCOUNT
REGION=ap-southeast-1
PREFIX=/travelian/gateway
USER=travelian-gateway

# 1. S3 bucket for admin uploads: objects are public images (logo, payment QR); listing is not.
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION"
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"PublicReadImages\",
  \"Effect\":\"Allow\",\"Principal\":\"*\",\"Action\":\"s3:GetObject\",\"Resource\":[\"arn:aws:s3:::$BUCKET/site/*\",\"arn:aws:s3:::$BUCKET/payment-qr/*\"]}]}"
aws s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration '{"CORSRules":[{"AllowedOrigins":["https://travelian.pk","https://www.travelian.pk","http://localhost:8080"],
  "AllowedMethods":["POST"],"AllowedHeaders":["*"],"MaxAgeSeconds":3000}]}'

# 2. Parameter Store: restricted airlines (edit any time, no deploy) and PayFast credentials (copied from Secrets Manager).
aws ssm put-parameter --region us-east-1 --name "$PREFIX/flights/restricted-airlines" --type String --value "EY,QR" --overwrite
PAYFAST=$(aws secretsmanager get-secret-value --region us-east-1 --secret-id travelian/payments/payfast --query SecretString --output text)
aws ssm put-parameter --region us-east-1 --name "$PREFIX/payments/payfast/merchant-id" --type SecureString --overwrite \
  --value "$(echo "$PAYFAST" | python3 -c 'import json,sys; print(json.load(sys.stdin)["merchantId"])')"
aws ssm put-parameter --region us-east-1 --name "$PREFIX/payments/payfast/secured-key" --type SecureString --overwrite \
  --value "$(echo "$PAYFAST" | python3 -c 'import json,sys; print(json.load(sys.stdin)["securedKey"])')"
unset PAYFAST

# 3. Least-privilege IAM user for the Lightsail server.
aws iam create-user --user-name "$USER"
aws iam put-user-policy --user-name "$USER" --policy-name gateway-runtime --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
  {\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::$BUCKET/site/*\",\"arn:aws:s3:::$BUCKET/payment-qr/*\",\"arn:aws:s3:::$BUCKET/payment-proof/*\"]},
  {\"Effect\":\"Allow\",\"Action\":[\"ses:SendEmail\",\"ses:SendRawEmail\"],\"Resource\":\"*\",\"Condition\":{\"StringLike\":{\"ses:FromAddress\":\"*@travelian.pk\"}}},
  {\"Effect\":\"Allow\",\"Action\":[\"ssm:GetParameter\",\"ssm:GetParameters\"],\"Resource\":\"arn:aws:ssm:us-east-1:$ACCOUNT:parameter$PREFIX/*\"},
  {\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\"],\"Resource\":\"*\",\"Condition\":{\"StringEquals\":{\"kms:ViaService\":\"ssm.us-east-1.amazonaws.com\"}}}]}"
echo "=== Paste these into /opt/travelian-api/shared/.env (then: sudo systemctl restart travelian-api) ==="
aws iam create-access-key --user-name "$USER" --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text \
  | awk '{print "AWS_ACCESS_KEY_ID=" $1; print "AWS_SECRET_ACCESS_KEY=" $2}'
cat <<EOF
AWS_REGION=us-east-1
SSM_PARAMETER_PREFIX=$PREFIX
ASSETS_BUCKET=$BUCKET
ASSETS_REGION=$REGION
ASSETS_PUBLIC_BASE_URL=https://$BUCKET.s3.$REGION.amazonaws.com
NOTIFY_EMAIL_ENABLED=true
NOTIFY_EMAILS=travelian.pk@gmail.com
EOF

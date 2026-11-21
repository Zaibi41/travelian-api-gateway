"""SES bounces and complaints from Amazon SNS (gateway#59): POST /v1/webhooks/ses.

No login: every message must carry a valid SNS signature for a topic in SES_SNS_TOPIC_ARNS (see
app.services.ses_feedback). Forged, unsigned or unknown-topic messages get 403 and change nothing.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.services import ses_feedback

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY = 256 * 1024  # SNS messages are at most 256 KB


@router.post("/ses", responses={400: {"description": "Not an SNS message."}, 403: {"description": "Not signed by SNS for our topic."}})
async def ses_feedback_webhook(request: Request):
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"detail": "Too large."}, status_code=413)
    try:
        return await run_in_threadpool(ses_feedback.handle, body)
    except ses_feedback.Refused as error:
        return JSONResponse({"detail": error.message}, status_code=error.status)

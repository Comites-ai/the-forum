# Troubleshooting Guide

Common issues and solutions for The Forum.

## Table of Contents

- [User-Facing Error Messages](#user-facing-error-messages)
- [Slack Integration Issues](#slack-integration-issues)
- [Vertex AI Issues](#vertex-ai-issues)
- [Firestore Issues](#firestore-issues)
- [GCS File Upload Issues](#gcs-file-upload-issues)
- [Local Development Issues](#local-development-issues)
- [Production Deployment Issues](#production-deployment-issues)

---

## User-Facing Error Messages

These are error messages that users see in Slack/Google Chat/Telegram when something goes wrong.

### "The first tool I called ({tool_name}) didn't respond at all..."

**When it appears**: The agent called a tool but never received a response from it.

**Common causes**:

1. **Tool lacks permissions**: The agent's service account doesn't have IAM permissions to call the underlying API
   - **Solution**: Grant the Reasoning Engine's service account the required roles (e.g., `roles/sheets.editor` for Google Sheets)

2. **Tool crashed**: The tool threw an exception before returning
   - **Solution**: Check the Reasoning Engine logs for stack traces

3. **Network/timeout issues**: The tool's external API call timed out
   - **Solution**: Check the external service status; consider adding retry logic in the tool

**How to investigate**:
```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND textPayload:"function_call=1 function_response=0"' \
  --project=vertex-ai-middleware-prod \
  --limit=20
```

The pattern `function_call=N function_response=0` means N tools were called but none responded.

---

### "One of my tools ({tool_name}) hit a rate limit..."

**When it appears**: A tool returned an error containing rate limit indicators (429, quota, etc.).

**Common causes**:

1. **External API rate limited**: The tool called an API that throttled requests
2. **Quota exceeded**: Project hit usage limits for an API

**Solutions**:

1. Wait a minute and try again
2. Check and increase quotas for the affected API
3. Add rate limiting/backoff logic in the tool

---

### "One of my tools ({tool_name}) doesn't have the access it needs..."

**When it appears**: A tool returned a 403/permission denied error.

**Common causes**:

1. **Missing IAM roles**: Service account lacks required permissions
2. **Resource not shared**: The resource (e.g., Google Sheet) isn't shared with the service account
3. **API not enabled**: The required GCP API isn't enabled in the project

**Solutions**:

1. Check the service account's IAM bindings
2. Share resources (Sheets, Docs, etc.) with the service account email
3. Enable the required API: `gcloud services enable <api>.googleapis.com`

---

### "Oh no, I appear to have a broken tool..."

**When it appears**: The agent called tools but the stream ended without producing any text response. This is a fallback message when no specific error type could be detected.

**Common causes**:

1. **Tool loop**: Agent keeps calling tools without summarizing results
   - **Solution**: Update agent prompt to require text responses after tool calls

2. **Token/iteration limits**: Agent ran out of budget mid-loop
   - **Solution**: Simplify the task or increase limits

3. **Tool returned confusing output**: Tool succeeded but output confused the agent
   - **Solution**: Check tool outputs in logs

---

### "I didn't like that request. Did you send me a file when I'm not set up for it? Or exceeded the character limit?"

**When it appears**: The agent returned an empty response with no tool calls (generic fallback).

**Common causes**:

1. **Agent doesn't handle images**: User sent an image but the agent isn't configured for multimodal input
   - **Solution**: Update agent to handle the `images` parameter — see Agent-Template's multimodal example, and [What The Forum Sends Your Agent](FOR_AGENT_DEVELOPERS.md#what-the-forum-sends-your-agent) for the message shape

2. **Agent crashed or timed out**: The agent encountered an error during processing
   - **Solution**: Check Vertex AI logs for errors:
     ```bash
     gcloud logging read "resource.type=aiplatform.googleapis.com/ReasoningEngine" --limit 50
     ```

3. **Message too long**: The input exceeded the model's context window
   - **Solution**: Send shorter messages or configure agent to handle truncation

4. **Agent returned non-text response**: Agent returned structured data instead of text
   - **Solution**: Ensure agent's `stream_query` yields text strings

### "I'm sorry, you tried to send me a file but I don't have any place to put it!"

**When it appears**: User sent a file attachment but the middleware couldn't upload it to GCS.

**Common causes**:

1. **GCS bucket doesn't exist or is misconfigured**
2. **Service account lacks write permissions**
3. **Network issues connecting to GCS**

**Solution**: See [GCS File Upload Issues](#gcs-file-upload-issues) section below.

### "Looks like Google won't let me think right now, try again in a minute."

**When it appears**: The middleware hit a Google API rate limit (HTTP 429).

**Common causes**:

1. **Too many requests**: High volume of messages in a short period
2. **Quota exceeded**: Project has hit Vertex AI API quotas

**Solutions**:

1. **Wait and retry**: Rate limits are temporary, usually clear within a minute

2. **Check quotas**:
   ```bash
   gcloud alpha services quota list --service=aiplatform.googleapis.com
   ```

3. **Request quota increase**: In GCP Console → IAM & Admin → Quotas

---

## Slack Integration Issues

### Messages appear separately instead of as a conversation thread

**Symptoms**: Each message exchange appears as a separate item. Messages show in "History" but the Chat view only shows the most recent message instead of a continuous conversation.

**Root Cause**: Your Slack app is configured as an "Agent or Assistant" in Slack's Agents & AI Apps settings.

**Solution**:
1. Go to https://api.slack.com/apps → Your app
2. Navigate to **"Agents & AI Apps"** in the left sidebar
3. **Disable** the "Agent or Assistant" configuration
4. Your DMs should now show as a normal conversation thread

**Why this happens**: Slack's "Agent or Assistant" mode is designed for one-off query assistants (like search bots) where each interaction is independent. It intentionally shows each exchange separately. For conversational bots that maintain context across messages, you should NOT use this mode.

---

### Bot doesn't respond to messages

**Symptoms**: Send a DM to bot, no response

**Checks**:

1. **Verify agent is registered in Firestore**:
   ```bash
   gcloud firestore documents list agents
   ```
   Ensure there's a document with your `slack_bot_id`

2. **Check Slack Events API is configured**:
   - Go to https://api.slack.com/apps → Your app → Event Subscriptions
   - Verify Request URL shows green checkmark ✓
   - Verify `message.im` is subscribed under "Subscribe to bot events"

3. **Check middleware logs**:
   ```bash
   # Local:
   # Watch terminal running uvicorn

   # Production:
   gcloud run logs read the-forum \
     --region us-central1 \
     --limit 50
   ```

4. **Verify bot token is valid**:
   ```bash
   # Test with Slack API
   curl https://slack.com/api/auth.test \
     -H "Authorization: Bearer xoxb-your-token"
   ```

### "URL verification failed" when configuring Events API

**Symptoms**: Slack shows error when setting Request URL

**Solutions**:

1. **Ensure The Forum is running**:
   ```bash
   # Local: Check uvicorn is running
   # Production: Check Cloud Run service is deployed

   # Test health endpoint
   curl https://YOUR_URL/health
   ```

2. **Check signing secret**:
   - Verify `.env` `SLACK_SIGNING_SECRET` includes your bot's signing secret
   - Multiple secrets are comma-separated (one per Slack app)
   - The new bot's secret must be added **before** configuring Event Subscriptions
   - Find each secret at: https://api.slack.com/apps → Your app → Basic Information

3. **For ngrok**: Ensure tunnel is active
   ```bash
   # Check ngrok status
   curl http://localhost:4040/api/tunnels
   ```

### Bot responds multiple times to a single message

**Symptoms**: Sending one message to the bot produces 2-3 identical (or similar) responses. Google Cloud logs show multiple separate Vertex AI sessions being created for the same message.

**Root Cause**: Slack retries event delivery if the webhook doesn't respond quickly enough (within ~3 seconds). Each retry is processed as a new event, creating a new Vertex AI session and sending another response.

**Solution**: The Forum handles this by checking for the `X-Slack-Retry-Num` header and immediately acknowledging retries without reprocessing. If you're seeing this issue, ensure your deployed version includes this retry handling in `app/api/v1/slack_events.py`.

**How to verify**: Check Cloud Run logs for entries like:
```
Acknowledging Slack retry #1 (reason: http_timeout)
```
If you don't see these log lines, your deployed version may be outdated. Redeploy The Forum.

---

### "Invalid signature" errors in logs

**Symptoms**: Middleware rejects Slack requests with 401

**Solutions**:

1. **Verify signing secret is included**:
   ```bash
   grep SLACK_SIGNING_SECRET .env
   ```
   `SLACK_SIGNING_SECRET` is comma-separated (one per Slack app). Ensure the secret for the bot receiving the error is in the list.

2. **Check system time** (for replay attack prevention):
   ```bash
   date
   # Should be accurate (use NTP)
   ```

3. **Restart The Forum** after changing signing secret

---

## Google Chat Integration Issues

### Bot shows "Not Responding" immediately after receiving messages

**Symptoms**: When you send a message to your Google Chat bot, you immediately see a "Bot Name Not Responding" notification, even though the bot processes the message and responds successfully in the background.

**Root Cause**: The Google Chat webhook endpoint is returning an incorrect response. Google Chat supports two response patterns:

1. **Synchronous response** (for quick replies < 30 seconds): Return `{"text": "Message"}` directly
2. **Asynchronous response** (for longer processing): Return `{}` and send message via Chat API

**You CANNOT do both.** If you return `{"text": "..."}` synchronously AND then send another message via the Chat API asynchronously, Google Chat shows "not responding" errors.

Any other format (like `{"status": "ok"}`, `{"success": true}`) is also invalid.

**Solution**: Choose the correct response pattern based on your processing time:

**For agents with background processing (common case):**
```python
# ✅ CORRECT - Return empty, send actual response via Chat API
background_tasks.add_task(process_message, ...)
return JSONResponse(content={})  # No synchronous text

# ❌ INCORRECT - Don't return text AND send via API
background_tasks.add_task(process_message, ...)
return JSONResponse(content={"text": "Processing..."})  # Causes "not responding"!
```

**For quick responses (< 30 seconds, no background tasks):**
```python
# ✅ CORRECT - Return synchronous response only
result = quick_process(message)
return JSONResponse(content={"text": result})  # Don't also call Chat API
```

**Always invalid:**
```python
# ❌ INCORRECT - Invalid formats
return JSONResponse(content={"status": "ok"})
return JSONResponse(content={"success": True})
```

**Where to check**: In [app/api/v1/google_chat_events.py](app/api/v1/google_chat_events.py), ensure you're using the pattern that matches your processing model.

---

## Vertex AI Issues

### "Agent not found" errors

**Symptoms**: Logs show agent not found when processing messages

**Solutions**:

1. **Check agent ID format** (most common issue):
   - For Reasoning Engines: `projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID`
   - For legacy agents: `projects/PROJECT/locations/LOCATION/agents/AGENT_ID`
   - Must be full resource name, not just the ID

2. **Verify Reasoning Engine exists**:
   ```bash
   # The gcloud ai agents command is for legacy agents
   # For Reasoning Engines, check via Python or Vertex AI Console
   ```

3. **Verify permissions**:
   ```bash
   gcloud projects get-iam-policy PROJECT_ID
   # Should have aiplatform.reasoningEngines.* permissions
   ```

### Empty or no response from agent

**Symptoms**: Bot responds but message is empty or generic error

**Solutions**:

1. **Test agent directly** in Vertex AI Console
   - Verify agent works independently

2. **Check agent deployment status**:
   ```bash
   gcloud ai agents describe AGENT_ID --location=us-central1
   ```

3. **Review Vertex AI quota**:
   - Check quotas in GCP Console
   - Vertex AI may be rate-limited

### Session creation fails

**Symptoms**: Errors creating Vertex AI sessions

**Solutions**:

1. **Check Vertex AI API is enabled**:
   ```bash
   gcloud services list --enabled | grep aiplatform
   ```

2. **Verify service account permissions**:
   - Cloud Run service account needs `aiplatform.sessions.create`

3. **Check project/location configuration**:
   ```bash
   grep GCP_ .env
   ```

---

## Scheduled Job Issues

### "My scheduled job *{job_name}* has not been working since..."

**When it appears**: User receives this notification when their scheduled job has failed 288 consecutive times (~24 hours with the default 5-minute dispatcher).

**Common causes**:

1. **Tool permissions issue**: The agent's tools lack required permissions
2. **External API down**: A tool depends on an unavailable service
3. **Agent prompt issue**: Agent gets stuck in a tool loop

**How to investigate**:

```bash
# Check the job's failure status in Firestore
gcloud firestore documents describe scheduled_jobs/JOB_ID \
  --project=vertex-ai-middleware-prod

# Look for consecutive_failures and last_error fields
```

**Solutions**:

1. Fix the underlying permission/tool issue (see error messages above)
2. The job will automatically recover on the next successful execution
3. To reset failure count manually, update the Firestore document:
   ```bash
   # Via Firestore console or script
   consecutive_failures: 0
   last_error: null
   ```

---

### Scheduled job runs but user doesn't receive message

**Symptoms**: Logs show job executed successfully but no message was sent.

**Check the logs for**:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND textPayload:"Job" AND textPayload:"failed"' \
  --project=vertex-ai-middleware-prod \
  --limit=20
```

**Common patterns**:

1. **`Tool 'X' did not respond (possible permission issue)`**: Tool lacks IAM permissions
2. **`Tool 'X' hit rate limit`**: External API throttled; will retry next scheduled run
3. **`Empty response (N chunks)`**: Agent returned no text; check agent prompt

**Viewing job failure history**:

```bash
# In Firestore, each scheduled_job document has:
# - consecutive_failures: number of failures since last success
# - last_error: description of the most recent failure
# - last_execution_at: timestamp of last successful execution
```

---

## Firestore Issues

### "Permission denied" errors

**Symptoms**: Can't read/write Firestore

**Solutions**:

1. **Enable Firestore API**:
   ```bash
   gcloud services enable firestore.googleapis.com
   ```

2. **Check authentication**:
   ```bash
   # Local:
   gcloud auth application-default login

   # Production: Verify service account has roles:
   # - roles/datastore.user (or roles/owner)
   ```

3. **Verify database exists**:
   ```bash
   gcloud firestore databases list
   ```

### "Database not found"

**Symptoms**: App requests fail with `404 The Firestore database does not exist`.

**Root Cause**: Firestore database hasn't been created in the project yet.

**Solution**: Firestore is provisioned by [`terraform/firestore.tf`](../terraform/firestore.tf); `terraform apply` (or `scripts/install.sh`) creates the `(default)` database. Collections are auto-created lazily by the app on first write — no separate init step. If you've already applied terraform and still hit this from local code, authenticate with ADC:

```bash
gcloud auth application-default login
```

### Agent not found in Firestore

**Symptoms**: `get_agent_by_bot_id` returns None, logs show "No agent found for bot_id: U..."

**Root Cause**: Using wrong ID format. Slack Events API sends `user_id` (U...) in authorizations, NOT `bot_id` (B...).

**Solutions**:

1. **Get the correct user_id** (this is the most common issue):
   ```bash
   # Get the user_id that Slack will send in events
   curl -s https://slack.com/api/auth.test \
     -H "Authorization: Bearer xoxb-your-token" | jq .user_id
   # Output: "U0AFZ86NE00" - use THIS, not the B... from Slack settings
   ```

2. **Verify agent document exists with correct ID**:
   ```bash
   gcloud firestore documents list --collection=agents
   # Check that slack_bot_id field has the U... ID
   ```

3. **Re-run deploy_agent.py with correct user_id**:
   ```bash
   python scripts/deploy_agent.py \
     --agent-name "..." \
     --vertex-ai-agent-id "..." \
     --slack-bot-id "U0AFZ86NE00" \
     --slack-bot-token "..."
   ```

**Why this happens**: The Slack app settings show a "Bot User ID" starting with `B`, but the Events API sends the `user_id` starting with `U` in the `authorizations` field. The middleware looks up agents using this `user_id`.

### Sessions not being created

**Symptoms**: New sessions don't appear in Firestore

**Solutions**:

1. **Check Firestore write permissions**
2. **Verify sessions collection exists**:
   ```bash
   gcloud firestore collections list
   ```

3. **Check logs for errors**:
   ```bash
   gcloud run logs read the-forum \
     --region us-central1 \
     --format json | grep session
   ```

---

## GCS File Upload Issues

### "I'm sorry, you tried to send me a file but I don't have any place to put it!"

**Symptoms**: User sends an image to the bot and receives this error message.

**Cause**: The Forum couldn't upload the file to GCS. This happens when:
- GCS bucket doesn't exist
- GCS bucket name is misconfigured
- Service account lacks permissions to write to the bucket
- Network connectivity issues to GCS

**Solutions**:

1. **Verify GCS bucket exists**:
   ```bash
   gcloud storage buckets describe gs://YOUR_BUCKET_NAME
   ```

2. **Check The Forum has correct bucket name**:
   ```bash
   grep GCS_BUCKET_NAME .env
   ```

3. **Verify IAM permissions**:
   ```bash
   # Get the service account
   PROJECT_NUMBER=$(gcloud projects describe PROJECT_ID --format="value(projectNumber)")
   SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

   # Check bucket IAM policy
   gcloud storage buckets get-iam-policy gs://YOUR_BUCKET_NAME | grep -A2 "$SA"
   ```

4. **Grant missing permissions** (if needed):
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://YOUR_BUCKET_NAME \
     --member="serviceAccount:${SA}" \
     --role="roles/storage.objectAdmin"
   ```

5. **Check The Forum logs for specific error**:
   ```bash
   gcloud run logs read the-forum \
     --region us-central1 \
     --limit 50 | grep -i "gcs\|upload\|bucket"
   ```

### Files upload but agent can't read them

**Symptoms**: The Forum logs show "Uploaded image to GCS" but agent returns errors or ignores the image.

**Solutions**:

1. **Verify agent has read access to bucket**:
   ```bash
   # If agent uses a different service account
   gcloud storage buckets add-iam-policy-binding gs://YOUR_BUCKET_NAME \
     --member="serviceAccount:AGENT_SA@PROJECT.iam.gserviceaccount.com" \
     --role="roles/storage.objectViewer"
   ```

2. **Test GCS access from agent environment**:
   ```bash
   # Verify the file exists
   gcloud storage ls gs://YOUR_BUCKET_NAME/slack-files/
   ```

3. **Check agent code handles `gcs_uri` field**:
   - Agent must check for `gcs_uri` in the images array
   - See [What The Forum Sends Your Agent](FOR_AGENT_DEVELOPERS.md#what-the-forum-sends-your-agent) for the message contract

### GCS bucket not configured (base64 fallback)

**Symptoms**: Logs show "Downloaded image (base64)" instead of "Uploaded image to GCS"

**Cause**: `GCS_BUCKET_NAME` environment variable is empty or not set.

**Solution**: Configure GCS bucket name:
```bash
# In .env file
GCS_BUCKET_NAME=your-project-slack-files

# Then restart the middleware
```

See [GCS Image Storage (Forum-operator setup)](FOR_AGENT_DEVELOPERS.md#gcs-image-storage-forum-operator-setup) for full setup instructions.

---

## Local Development Issues

### ngrok tunnel not working

**Symptoms**: Can't access localhost via ngrok URL

**Solutions**:

1. **Verify ngrok is installed and authenticated**:
   ```bash
   ngrok version
   ngrok config check
   ```

2. **Check tunnel is active**:
   ```bash
   curl http://localhost:4040/api/tunnels
   ```

3. **Restart ngrok**:
   ```bash
   ngrok http 8080
   ```

4. **For Linux VM**: Ensure VM can make outbound connections

### Firestore emulator not connecting

**Symptoms**: Local dev can't connect to Firestore emulator

**Solutions**:

1. **Start emulator**:
   ```bash
   gcloud emulators firestore start --host-port=0.0.0.0:8681
   ```

2. **Set environment variable**:
   ```bash
   export FIRESTORE_EMULATOR_HOST=localhost:8681
   ```

3. **Verify emulator is running**:
   ```bash
   curl http://localhost:8681
   ```

### Module import errors

**Symptoms**: `ModuleNotFoundError` when running app

**Solutions**:

1. **Activate virtual environment**:
   ```bash
   source venv/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Verify Python version**:
   ```bash
   python --version  # Should be 3.11 or 3.12
   ```

### "ModuleNotFoundError: No module named 'aiohttp'"

**Symptoms**: Error when running the app, Slack async client fails

**Solution**: Install aiohttp (should be in requirements.txt):
```bash
pip install aiohttp
```

### pip install takes forever or hangs

**Symptoms**: `pip install -r requirements.txt` runs for many minutes with "Resolving dependencies" messages

**Cause**: The google-cloud-aiplatform package has complex dependencies that pip needs to resolve.

**Solutions**:

1. **Use pinned versions** (already in requirements.txt):
   ```bash
   pip install -r requirements.txt
   ```

2. **If still slow, try upgrading pip**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Clear pip cache if having issues**:
   ```bash
   pip cache purge
   pip install -r requirements.txt
   ```

---

## Production Deployment Issues

### Cloud Run deployment fails

**Symptoms**: `gcloud run deploy` fails

**Solutions**:

1. **Check Cloud Run API is enabled**:
   ```bash
   gcloud services enable run.googleapis.com
   ```

2. **Verify Docker builds**:
   ```bash
   docker build -t test .
   ```

3. **Check build logs**:
   ```bash
   gcloud builds list --limit 5
   gcloud builds log BUILD_ID
   ```

### Service crashes on startup

**Symptoms**: Cloud Run service won't start

**Solutions**:

1. **Check environment variables**:
   ```bash
   gcloud run services describe the-forum \
     --region us-central1 \
     --format yaml
   ```

2. **Verify secrets are set**:
   ```bash
   gcloud secrets list
   gcloud secrets versions list slack-signing-secret
   ```

3. **Review startup logs**:
   ```bash
   gcloud run logs read the-forum \
     --region us-central1 \
     --limit 100
   ```

### Secrets not accessible

**Symptoms**: Cloud Run can't access Secret Manager secrets

**Solutions**:

1. **Grant permissions**:
   ```bash
   PROJECT_NUMBER=$(gcloud projects describe PROJECT_ID --format="value(projectNumber)")

   gcloud secrets add-iam-policy-binding slack-signing-secret \
     --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"
   ```

2. **Verify secret exists**:
   ```bash
   gcloud secrets describe slack-signing-secret
   ```

3. **Redeploy after fixing permissions**

---

## Admin UI Issues

### Sign-in works but I land on "You shall not pass."

**Symptoms**: OAuth completes, then the admin UI shows the forbidden page
with your email and the required role.

**Cause**: Your Google account does not hold the required role
(`roles/owner` by default) as a **direct** binding on `GCP_PROJECT_ID`.
Inherited roles from folder or org bindings are intentionally not honored.

**Solutions**:

1. Grant yourself a direct project binding:
   ```bash
   gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
     --member="user:you@example.com" \
     --role="roles/owner"
   ```
2. Or override the required role to one you already hold directly by
   setting `ADMIN_REQUIRED_ROLE=roles/editor` (or similar) on the Cloud Run
   service and redeploying.

### `redirect_uri_mismatch` from Google during sign-in

**Symptoms**: Google's OAuth consent screen shows a
`redirect_uri_mismatch` error.

**Cause**: The redirect URI registered on your OAuth client ID doesn't
match `OAUTH_REDIRECT_URI` in your environment.

**Solutions**:

1. In GCP Console → APIs & Services → Credentials → your OAuth client, add
   the exact URL from `OAUTH_REDIRECT_URI` to "Authorized redirect URIs."
2. Google accepts `http://localhost` for local dev but **not**
   `http://127.0.0.1`. Switch to `localhost` if you used the IP form.

### Last-error card shows nothing even though errors happened

**Symptoms**: Agent detail page shows "No recent errors" but Cloud Run
logs clearly show ERROR-level entries for the agent.

**Causes & solutions**:

1. **Cloud Logging API not enabled** for the project:
   ```bash
   gcloud services enable logging.googleapis.com
   ```
2. **Service name mismatch.** The filter scopes by
   `resource.labels.service_name="$CLOUD_RUN_SERVICE_NAME"` (default
   `the-forum`). If your Cloud Run service has a different name, set
   `CLOUD_RUN_SERVICE_NAME` on the service and redeploy.
3. **Log entry doesn't mention `agent_id`.** The filter matches either
   `jsonPayload.agent_id="<id>"` or `textPayload:"<id>"`. If logs reference
   the agent only by display name, the filter won't match.

---

## Getting Help

If you've tried the above and still have issues:

1. **Check full logs**:
   ```bash
   gcloud run logs read the-forum \
     --region us-central1 \
     --format json \
     --limit 100 > logs.json
   ```

2. **Verify all components**:
   - Slack app configured correctly
   - Vertex AI agent deployed and working
   - Firestore has correct agent documents
   - Cloud Run service running

3. **Test components individually**:
   - Test Slack Events API with simple endpoint
   - Test Vertex AI agent in Console
   - Test Firestore access with gcloud

4. **Open GitHub issue** with:
   - Error messages
   - Relevant log snippets
   - Steps to reproduce

# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

#!/usr/bin/env python3
"""Test a scheduled job via API."""
import requests
from google.cloud import firestore

# Get a scheduled job ID from Firestore
db = firestore.Client(project='vertex-ai-middleware-prod', database='(default)')
jobs_ref = db.collection('scheduled_jobs')
jobs = list(jobs_ref.limit(1).stream())

if not jobs:
    print("No scheduled jobs found")
    exit(1)

job = jobs[0]
job_id = job.id
job_data = job.to_dict()

print(f"Found scheduled job: {job_id}")
print(f"Job data: {job_data}")
print(f"\nTesting job via API...")

# Test the job
url = f"https://slack-vertex-middleware-mqwj7cavdq-uc.a.run.app/api/v1/scheduled-jobs/{job_id}/test"
response = requests.post(url)

print(f"\nResponse status: {response.status_code}")
print(f"Response body: {response.text}")

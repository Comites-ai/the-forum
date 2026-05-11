# Contributing to The Forum

Welcome to The Forum by Comites.ai!

## About the Project

The name "Comites" comes from the advisors who counseled Roman emperors. In our vision, users create AI agents (comites) that advise and assist them. "The Forum" is the place where users interact with their comites—a platform that connects messaging platforms like Slack, Google Chat, and Telegram to AI agents powered by Google Vertex AI.

We're excited to have you contribute to this open-source project!

## Why AGPL-3.0?

We've chosen the AGPL license to keep Comites.ai's "The Forum" an open-source platform that anyone can build agents on.

As part of this license, anyone who improves The Forum is required to contribute those improvements back to this codebase—so we can all benefit from each other's work.

I have no intention of ever making this software proprietary. My goal is to ensure we can all have fun writing our agents and running them in an open-source environment!

## Contributor License Agreement (CLA)

All contributors must sign our Contributor License Agreement before their first pull request can be merged. We use [CLA Assistant](https://cla-assistant.io/) to handle the signing.

You can read the full CLA here: <https://gist.github.com/Jonathan-Comites/5825b5747f2446c9c4f973989858001f>

### Why a CLA?

The CLA ensures that Comites.ai has the rights to keep your contributions as part of this open-source project permanently. Without a CLA, contributors would retain sole copyright over their code and could potentially ask us to remove it later.

By signing the CLA, you're granting Comites.ai a license to use your contributions as part of The Forum—while you still retain your own copyright and can use your code however you like.

### How to Sign

When you open your first pull request, CLA Assistant will post a comment with a link. Follow the link, sign in with GitHub, and click the button to agree. **The CLA is effective the moment you do that** — your PR is unblocked immediately.

### Supplemental Information by Email

CLA Assistant records your GitHub username, email, and agreement timestamp. There is some additional information we'd like to have on file even though the CLA is already in force. Please email it to **cla@comites.ai** at your convenience. Use a subject line like `CLA supplemental info — <your GitHub username>` so we can match it to your signature.

Always include:

- **Full legal name** — the name CLA Assistant captured is your GitHub display name, which may not match your legal name.

Include the following only **if Section 4 of the CLA applies to you** (i.e., you are contributing on behalf of an employer or other entity):

- **Employer full legal name**
- **Your title or role at Employer**
- **Basis for your authority to bind** the employer to the CLA (e.g., signed delegation, role-based authority, employment policy, written approval from a specific authorized person, etc.)

### Contributing as Part of Your Job

If you are contributing to this codebase as part of your employment, we assume that when you sign the CLA, you are also signing it on behalf of your company. **You are responsible for ensuring you have the proper approvals from your employer to do so**, and we ask you to document this in your supplemental email (above).

If your company would prefer to have a formal Corporate CLA in place, please contact us at cla@comites.ai to arrange that.

## How to Contribute

### Reporting Issues

- Use GitHub Issues to report bugs or request features
- Search existing issues before creating a new one
- Provide as much detail as possible: steps to reproduce, expected behavior, actual behavior

### Submitting Changes

1. **Fork the repository** and create a new branch from `main`
2. **Make your changes** following our code standards (below)
3. **Test your changes** thoroughly
4. **Submit a pull request** with a clear description of what you've done
5. **Sign the CLA** when prompted by CLA Assistant, and email cla@comites.ai with the supplemental information described above

### Pull Request Guidelines

- Keep PRs focused on a single change
- Write clear commit messages
- Update documentation if needed
- Ensure that `scripts/install.sh`, `scripts/deploy_forum.sh`, and `scripts/uninstall.sh` still operate successfully against a clean GCP project.

## Development Setup

### Prerequisites

- Python 3.11+
- Google Cloud SDK
- Access to a Google Cloud project with Vertex AI enabled

### Local Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/the-forum.git
cd the-forum

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
# Edit .env with your configuration
```

### Running Locally

```bash
uvicorn app.main:app --reload
```

### Running Tests

Install dev dependencies (includes `pytest`, `pytest-asyncio`, and `httpx`):

```bash
pip install -r requirements-dev.txt
```

Then run the suite:

```bash
pytest
```

The tests use **in-memory fakes** for Firestore, Vertex AI, GCS, and the platform connectors — they don't touch real GCP or Slack. See [tests/fakes/](tests/fakes/) for the fakes and [tests/conftest.py](tests/conftest.py) for the shared fixtures (`fake_firestore`, `fake_vertex_ai`, `client`, `slack_signed_request`, etc.). When adding new tests, copy the patterns from existing ones in [tests/services/](tests/services/) and [tests/api/](tests/api/).

### Continuous Integration

Every pull request automatically runs three checks:

| Check | What it does |
|---|---|
| **Python tests** | `pytest` against the suite in `tests/` |
| **Shell lint** | `bash -n` syntax check + `shellcheck -S error` on `scripts/*.sh` |
| **Terraform lint** | `terraform fmt -check -recursive terraform/` |

Workflow lives at [.github/workflows/ci.yml](.github/workflows/ci.yml). You can run the same checks locally:

```bash
pytest                                    # python tests
shellcheck -S error scripts/*.sh          # apt install shellcheck if needed
bash -n scripts/*.sh                      # syntax check
terraform fmt -check -recursive terraform/ # terraform formatting
```

Live infrastructure tests (running `install.sh` against a real GCP project) are not automated — maintainers run them manually for PRs that touch `scripts/install.sh`, `scripts/uninstall.sh`, or `terraform/`.

## Code Standards

### Style

- Follow PEP 8 for Python code
- Use type hints where possible
- Keep functions focused and reasonably sized
- Write docstrings for public functions and classes

### Architecture

- Platform connectors go in `app/services/platforms/`
- API endpoints go in `app/api/v1/`
- Data models go in `app/models/`
- Pydantic schemas go in `app/schemas/`

### Commits

- Write clear, descriptive commit messages
- Use present tense ("Add feature" not "Added feature")
- Reference issue numbers when applicable

## Questions?

If you have questions about contributing, feel free to open an issue or reach out to the maintainers.

Thank you for contributing to The Forum!

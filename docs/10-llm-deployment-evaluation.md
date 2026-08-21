# LLM Deployment and Evaluation

UrbanMend supports OpenAI-compatible chat-completions APIs. Production deployments must provide
the provider explicitly; the default remains the deterministic keyword fallback.

## Configuration

Set these values in the deployment secret/configuration store, not in committed files:

```dotenv
CLASSIFICATION_LLM_PROVIDER=openai_compatible
CLASSIFICATION_LLM_ENDPOINT=https://api.openai.com/v1
CLASSIFICATION_LLM_API_KEY=<secret>
CLASSIFICATION_LLM_MODEL=gpt-4o-mini
CLASSIFICATION_LLM_TIMEOUT_SECONDS=10
CLASSIFICATION_LLM_MAX_ATTEMPTS=2
CLASSIFICATION_LLM_MAX_OUTPUT_TOKENS=300
CLASSIFICATION_LLM_DAILY_TOKEN_BUDGET=1000000
CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD=0.70
```

Use an API account whose submitted data is not used for model training. Keep the endpoint HTTPS,
use a project-scoped key, set a provider-side spend limit, and rotate the key after accidental
exposure. The application sends report text, language, and the active category slugs; it does not
send user identity, coordinates, address, or contact fields.

Before enabling traffic, run a provider smoke evaluation inside the API container:

```powershell
docker compose exec -T api python manage.py evaluate_classifier `
  docs/evaluation/classification-sample.jsonl --classifier llm
```

## Evaluation Dataset

The committed sample is a smoke set, not evidence for the 85% acceptance claim. Build a held-out,
human-reviewed JSONL dataset with at least 100 examples, balanced across the seven categories, four
severity bands, and English/Bangla/code-mixed reports. Do not copy production PII into it.

Each line has this schema:

```json
{"text":"...","language":"bn","expected_category":"roads","expected_severity":"high"}
```

Freeze the dataset before prompt/model tuning. Keep a separate development set for tuning so the
held-out score remains meaningful. Have two reviewers label severity independently and adjudicate
disagreements, since severity agreement cannot exceed label quality.

Run the release gate with the PRD's category target and the initial severity target:

```powershell
docker compose exec -T api python manage.py evaluate_classifier `
  path/to/held-out.jsonl --classifier llm --category-target 0.85 `
  --severity-target 0.80 --fail-below-target
```

Archive the JSON output with the model name, prompt/code revision, date, and dataset revision. A
model or prompt change requires rerunning the same held-out set. Investigate results per category
and severity even when the aggregate gate passes; a balanced aggregate can hide a life-safety
regression in the Critical band.

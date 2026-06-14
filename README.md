# Temporal Jellyfin Workflows

A [Temporal](https://temporal.io) workflow that fetches your Jellyfin watch history and favorites,
then uses an OpenAI-compatible LLM to produce personalised film and TV recommendations.

## How it works

The `RecommendationsWorkflow` runs six Temporal activities in parallel to fetch from Jellyfin:

- Favorites (movies + series)
- Watched movies / series
- Unwatched movies / series
- In-progress series (via Jellyfin's Next Up API)

The results are assembled into a prompt and sent to an LLM via the OpenAI Agents SDK. The workflow
returns the model's recommendation text as its result.

## Configuration

The worker is configured entirely through environment variables:

| Variable | Description |
|---|---|
| `JELLYFIN_URL` | Base URL of your Jellyfin server (e.g. `http://jellyfin:8096`) |
| `JELLYFIN_API_KEY` | Jellyfin API token |
| `JELLYFIN_USER_ID` | User ID (UUID) or username to fetch data for; auto-detects the first user if unset |
| `OPENAI_BASE_URL` | OpenAI-compatible API base URL (e.g. `http://localhost:8080/v1` for `llama.cpp`) |
| `OPENAI_API_KEY` | API key (not needed for local models) |
| `RECOMMENDER_MODEL` | Model name passed to the agent (default: `gpt-4o`) |
| `TEMPORAL_ADDRESS` | Temporal frontend address (default: `localhost:7233`) |
| `TEMPORAL_NAMESPACE` | Temporal namespace (default: `default`) |
| `TEMPORAL_TASK_QUEUE` | Task queue name (default: `recommendations-queue`) |

## Running

### Temporal server

```bash
nix-shell -p temporal-cli --run "temporal server start-dev"
```
### With Nix

```bash
# Enter the dev shell
nix develop

# Run the worker directly
JELLYFIN_URL=http://localhost:8096 \
JELLYFIN_API_KEY=<token> \
JELLYFIN_USER_ID=<uuid> \
OPENAI_BASE_URL=http://localhost:8080/v1 \
OPENAI_API_KEY=not-needed \
RECOMMENDER_MODEL=gemma4:e4b \
python recommender-worker.py
```

### Triggering a workflow

```bash
temporal workflow start \
  --namespace jellyfin-rec \
  --type RecommendationsWorkflow \
  --task-queue recommendations-queue \
  --workflow-id my-recommendations

temporal workflow result \
  --namespace jellyfin-rec \
  --workflow-id my-recommendations
```

## Testing

The NixOS VM test spins up the following in a set of VMs:

* Jellyfin
* `llama.cpp` server (using `Gemma4 E2B QAT`)
* Temporal
* The recommendation workflow worker

A small movie and TV library is seeded, marked with watched/favorite states, the workflow is then
triggered, and asserts it completes with a non-empty result.

```bash
nix flake check -L
```

To test against a local GGUF model instead of downloading one:

```nix
# In your local flake override:
model = /path/to/your/model.gguf;
```

# Temporal Jellyfin Workflows

[Temporal](https://temporal.io) workflows that connect your Jellyfin library to an
OpenAI-compatible LLM. These workflows are provided:

- **`RecommendationsWorkflow`**: personalised film and TV recommendations based on your watch
  history and favorites
- **`MissingSeasonsWorkflow`**: identifies incomplete TV series in your library, distinguishing
  gap seasons (blocking a continuous run) from trailing seasons (newer releases not yet collected)
- **`DirectorCompletenessWorkflow`**: finds directors well-represented in your movie collection
  and surfaces the gaps in their filmography
- **`WorldExplorerWorkflow`**: takes a language, nationality, or country and recommends
  world cinema and TV matching that target

## How it works

### RecommendationsWorkflow

Six Temporal activities run in parallel to fetch from Jellyfin:

- Favorites (movies + series)
- Watched movies / series
- Unwatched movies / series
- In-progress series (via Jellyfin's Next Up API)

The results are assembled into a prompt and sent to an LLM via the OpenAI Agents SDK. The workflow returns the model's
recommendation text as its result.

### MissingSeasonsWorkflow

1. Fetches all series from your Jellyfin library along with their owned season numbers
2. Looks up the full season list for each series from TMDB (preferred) or TVMaze, including
   premiere dates
3. Computes missing seasons, split into:
   - **Gap**: seasons between ones you own, blocking a continuous viewing run
   - **Trailing**: newer seasons you have not yet collected
4. Seasons without a known premiere date are labelled `(TBA)`; the agent is instructed to treat
   those and future-dated seasons as upcoming rather than simply missing
5. The report is sent to an LLM which summarises what to acquire, prioritising gap seasons


### DirectorCompletenessWorkflow

1. Fetches all movies from your Jellyfin library with their director metadata
2. Identifies directors with at least 2 films in your collection
3. For each director, resolves a TMDB person ID - checking Jellyfin's stored provider IDs
   first, falling back to a TMDB name search
4. Fetches each director's full filmography from TMDB in parallel
5. Computes missing films using your full movie library as the exclusion set (not just films
   Jellyfin has attributed to that director, to handle incomplete metadata)
6. The report is sent to an LLM which summarises the gaps, prioritising directors where you
   are closest to a complete collection

### WorldExplorerWorkflow

Takes a freeform target as workflow input (a language such as `"Japanese"`, a nationality
such as `"Belgian"`, or a country such as `"Brazil"`) and:

1. Runs a validation agent that resolves the input to a canonical label and ISO 639-2 codes.
   Countries and nationalities with a small set of official languages are resolved to all of
   them (e.g. `"Belgian"` → French + Dutch). Inputs that are too ambiguous (e.g. `"Indian"`)
   return a helpful suggestion naming the specific languages to try instead
2. Runs five Temporal activities in parallel to fetch the full Jellyfin library: favorites,
   watched movies, watched series, unwatched movies, and unwatched series
3. Passes the full library and the resolved target label to a discovery agent, which
   recommends 10-15 titles matching the target, blending unwatched titles already in the
   library with fresh discoveries, tailored to the user's taste from their watch history and
   favourites

## Configuration

All workers share most environment variables. `TEMPORAL_TASK_QUEUE` differs between them.

| Variable | Description |
|---|---|
| `JELLYFIN_URL` | Base URL of your Jellyfin server (e.g. `http://jellyfin:8096`) |
| `JELLYFIN_API_KEY` | Jellyfin API token |
| `JELLYFIN_USER_ID` | User ID (UUID) or username to fetch data for; auto-detects the first user if unset |
| `OPENAI_BASE_URL` | OpenAI-compatible API base URL (e.g. `http://localhost:8080/v1` for `llama.cpp`) |
| `OPENAI_API_KEY` | API key (not needed for local models) |
| `RECOMMENDER_MODEL` | Model name passed to the agent (default: `gpt-4o`) |
| `TMDB_API_KEY` | TMDB API key; required for `DirectorCompletenessWorkflow`, optional for `MissingSeasonsWorkflow` (falls back to TVMaze) |
| `TEMPORAL_ADDRESS` | Temporal frontend address (default: `localhost:7233`) |
| `TEMPORAL_NAMESPACE` | Temporal namespace (default: `default`) |
| `TEMPORAL_TASK_QUEUE` | Task queue name (see per-worker defaults below) |

## Running

### Temporal server

```bash
nix-shell -p temporal-cli --run "temporal server start-dev"
```

### With Nix

```bash
nix develop

# Recommendations worker (task queue: recommendations-queue)
JELLYFIN_URL=http://localhost:8096 \
JELLYFIN_API_KEY=<token> \
JELLYFIN_USER_ID=<uuid> \
OPENAI_BASE_URL=http://localhost:8080/v1 \
OPENAI_API_KEY=not-needed \
RECOMMENDER_MODEL=gemma4:e2b \
python recommender-worker.py

# Missing seasons worker (task queue: missing-seasons-queue)
JELLYFIN_URL=http://localhost:8096 \
JELLYFIN_API_KEY=<token> \
JELLYFIN_USER_ID=<uuid> \
TMDB_API_KEY=<tmdb-key> \
OPENAI_BASE_URL=http://localhost:8080/v1 \
OPENAI_API_KEY=not-needed \
RECOMMENDER_MODEL=gemma4:e2b \
python missing-seasons-worker.py

# Director completeness worker (task queue: director-completeness-queue)
JELLYFIN_URL=http://localhost:8096 \
JELLYFIN_API_KEY=<token> \
JELLYFIN_USER_ID=<uuid> \
TMDB_API_KEY=<tmdb-key> \
OPENAI_BASE_URL=http://localhost:8080/v1 \
OPENAI_API_KEY=not-needed \
RECOMMENDER_MODEL=gemma4:e2b \
python director-completeness-worker.py

# World explorer worker (task queue: world-explorer-queue)
JELLYFIN_URL=http://localhost:8096 \
JELLYFIN_API_KEY=<token> \
JELLYFIN_USER_ID=<uuid> \
OPENAI_BASE_URL=http://localhost:8080/v1 \
OPENAI_API_KEY=not-needed \
RECOMMENDER_MODEL=gemma4:e2b \
python world-explorer-worker.py
```

### Triggering workflows

```bash
# Recommendations
temporal workflow start \
  --type RecommendationsWorkflow \
  --task-queue recommendations-queue \
  --workflow-id my-recommendations

temporal workflow result --workflow-id my-recommendations

# Missing seasons
temporal workflow start \
  --type MissingSeasonsWorkflow \
  --task-queue missing-seasons-queue \
  --workflow-id my-missing-seasons

temporal workflow result --workflow-id my-missing-seasons

# Director completeness
temporal workflow start \
  --type DirectorCompletenessWorkflow \
  --task-queue director-completeness-queue \
  --workflow-id my-director-completeness

temporal workflow result --workflow-id my-director-completeness

# World explorer (pass a language, nationality, or country as JSON input)
temporal workflow start \
  --type WorldExplorerWorkflow \
  --task-queue world-explorer-queue \
  --workflow-id my-world-explorer \
  --input '"Japanese"'

temporal workflow result --workflow-id my-world-explorer
```

## Testing

The NixOS VM test spins up the following in a set of VMs:

* Jellyfin
* `llama.cpp` server (using `Gemma4 E2B QAT`)
* Temporal
* The workflow workers

A small movie and TV library is seeded with watched/favorite states, the workflows are triggered,
and the test asserts they complete with non-empty results.

```bash
nix flake check -L
```

To test against a local GGUF model instead of downloading one:

```nix
# In your local flake override:
model = /path/to/your/model.gguf;
```

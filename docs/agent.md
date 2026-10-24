# The refresh agent

An open-weight LLM keeps records current by re-reading each facility's **own website** and proposing
corrections. It is a reader, never an author.

```
pick stalest facilities with a website ─▶ fetch homepage + contact/about/insurance pages
   (robots.txt, 1 req/s/host)                       │
                                                    ▼
                              open model extracts {field, verbatim evidence quote}
                                                    │
                              evidence quote found in the page text?  ── no ──▶ discarded
                                                    │ yes
                         ┌──────────────────────────┴───────────────────────────┐
           sources/staging/agent_web.jsonl (trust 0.6)            PATCH /v1/facilities/{id}
           → cura merge → validate → PR                            → moderator proposal queue
```

## Running it

Any OpenAI-compatible endpoint serving an open-weight model works:

```bash
# local, with Ollama
ollama pull qwen2.5:7b-instruct
CURA_LLM_BASE_URL=http://localhost:11434/v1 CURA_LLM_MODEL=qwen2.5:7b-instruct uv run cura agent run --limit 25

# hosted open model (Together / Groq / OpenRouter / HF Inference Endpoints / your own vLLM)
CURA_LLM_BASE_URL=https://api.together.xyz/v1 CURA_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct-Turbo \
CURA_LLM_API_KEY=... uv run cura agent run
```

Recommended models: Qwen2.5-7B/14B-Instruct (strong Arabic + JSON), Llama-3.1-8B-Instruct.
Temperature is fixed at 0.

## Schedule

`.github/workflows/refresh.yml` runs the scripted collectors daily and the agent every Friday (or on
manual dispatch), then opens a pull request. Set repository secrets `CURA_LLM_BASE_URL` and
`CURA_LLM_API_KEY` and the variable `CURA_LLM_MODEL`.

## Why it cannot corrupt the dataset

* Values without a verbatim, present-in-page evidence quote are dropped (`agent.runner.grounded`).
* Its trust prior (0.6) is below every official source, so it fills gaps rather than overriding.
* The merge re-validates every record against the schema; CI re-validates the PR.
* In API mode its edits are proposals from a normal user account — a moderator decides.

"""Daily morning economic & current-affairs news briefing automation.

Package layout:
  model       - common Article contract (shared by all modules)
  config      - env/secrets + tunables
  http        - shared HTTP + feed fetch/validate/parse
  state       - repo-backed sent-log + cache
  feeds       - feed URL registry
  collectors/ - source A (current affairs), B (economy), C (crypto)
  normalize   - raw -> common model cleanup
  prefilter   - keyword hard filter (drop 연예/스포츠, keep 라이프)
  dedup       - canonical URL + title shingling + over-merge guards
  select      - single-call LLM selection with diversity caps
  summarize   - single-call structured summary with evidence
  render      - Korean briefing formatting
  telegram    - delivery with 4096-char splitting
  fallback    - deterministic minimal path (no LLM)
  pipeline    - orchestration with automatic fallback
"""

__version__ = "1.0.0"

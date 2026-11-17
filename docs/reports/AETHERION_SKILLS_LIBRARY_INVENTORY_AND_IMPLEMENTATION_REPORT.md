# Aetherion Skills Library Inventory and Implementation Report

Date: 2026-10-17  
Scope: `cli/aetherion_cli/skills/**/SKILL.md`, CLI Skills Library, TUI skill controls, backend skill lifecycle/runtime paths.

## Executive result

The user-controlled Skills Library is now available as a local, offline-safe catalogue and user-scope store:

- **192** packaged `SKILL.md` definitions were discovered.
- **192** normalized IDs were produced; **0** duplicate normalized IDs were found.
- Packaged definitions are **browse-only until explicit install**. Listing does not copy them to user storage.
- Local install state is persisted under `~/.aetherion/skills-library` by default and includes an artifact path, version, and SHA-256 digest.
- Local lifecycle operations are available through the single `skills` CLI group and TUI: browse/search/filter, install, enable, disable, uninstall, custom create/edit/delete, detail, and Markdown export.
- Custom skill content is stored as data and starts disabled. It does not grant tools, permissions, packages, subprocesses, or execution.
- Backend `SkillEngine` now supports a closed explicit ID allow-list (`selected_skill_ids`) while retaining enabled-skill and tenant checks.
- Backend skill preview ownership, expiry, permission approval, bounded UTF-8 Markdown parsing, and redirect validation were hardened.

The local filesystem library is intentionally **not falsely reported as runtime-connected**. A local definition is not automatically uploaded into a remote tenant's backend store. Backend-managed runtime skills remain tenant-scoped and require the existing backend lifecycle/API path. The local catalogue entries therefore report `definition_only_not_runtime_wired` until an explicit, authenticated attach/publish flow is implemented.

## Implementation map

| Area | Evidence |
|---|---|
| Local catalogue/state | `cli/aetherion_cli/skill_library.py` (`SkillLibrary`, `SkillEntry`) |
| CLI surface | `cli/aetherion_cli/main.py` — one `skills` group; local commands plus explicit `runtime-*` compatibility commands |
| TUI surface | `cli/aetherion_cli/tui/screens/skills.py`, `tui/gateway.py`, `tui/state.py` |
| No list-time bootstrap | `tui/skill_creator.py:list_local_skills()` now only lists legacy local files; bundled catalogue is not copied implicitly |
| Runtime allow-list | `backend/execution/extensions/skill_engine.py:match_and_activate()` and `backend/execution/agent_runtime/runtime.py` |
| Backend lifecycle hardening | `backend/execution/extensions/manager.py`, `extensions/fetcher.py` |
| Direct library tests | `cli/tests/test_skill_library.py` |
| Existing CLI/creator regression tests | `cli/tests/test_cli.py`, `cli/tests/test_skill_creator.py`, `cli/tests/test_tui_state.py` |
| Backend lifecycle tests | `tests/integration/test_phase3_extensions.py` |

## Inventory evidence

The inventory scan uses every `SKILL.md` below, derives the ID from frontmatter `name` or the containing directory, lowercases and canonicalizes it, and records a short content digest. `dependency_metadata_present` means the document contains a recognizable dependency/requirement declaration; it does **not** mean the dependency was installed or verified.

| Count | Result |
|---|---:|
| Raw `SKILL.md` files | 192 |
| Normalized unique IDs | 192 |
| Duplicate normalized IDs | 0 |
| Dependency metadata detected | 42 |
| No dependency metadata detected | 150 |
| Local runtime-connected entries | 0 (intentional, truthful status) |
| Local definition-only entries | 192 |

Category counts: `others` 51, `integrations` 28, `design` 24, `ai-ml` 21, `development` 21, `tool-development` 15, `pm-product` 8, `data-analysis` 8, `documentation` 5, `frameworks` 3, `security-testing` 3, and one each in `baidu-image-crawler`, `daily-review`, `fenglin-workflow`, `programming-workflow`, and `project-standards`.

The existing `skill-quality-scores.json` reports 158 skills and is not used as the source of truth because it does not match the shipped `SKILL.md` tree. The file remains preserved as existing repository data.

## Per-definition classification inventory

Classification for every packaged row is `bundled / definition_only_not_runtime_wired`: the definition is browseable and installable as data, but it is not proof of a runtime tool, package, provider, MCP server, subprocess, or backend tenant installation. The digest is a reproducibility aid and must be recalculated after any source edit.

| ID | Category | Relative path | Dependency scan | SHA-256 prefix | Classification |
|---|---|---|---|---|---|
| biopython | ai-ml | ai-ml/biopython/SKILL.md | dependency_metadata_present | 9079af86248f | bundled / definition_only_not_runtime_wired |
| claude-mem | ai-ml | ai-ml/claude-mem/SKILL.md | no_declared_dependency_metadata | 96c68f744ef0 | bundled / definition_only_not_runtime_wired |
| vercel-deploy | ai-ml | ai-ml/claude.ai/vercel-deploy-claimable/SKILL.md | no_declared_dependency_metadata | 31a4a5b49db7 | bundled / definition_only_not_runtime_wired |
| dispatching-parallel-agents | ai-ml | ai-ml/dispatching-parallel-agents/SKILL.md | no_declared_dependency_metadata | c628917689cc | bundled / definition_only_not_runtime_wired |
| gemini | ai-ml | ai-ml/gemini/SKILL.md | no_declared_dependency_metadata | 29205943f8ad | bundled / definition_only_not_runtime_wired |
| hugging-face-cli | ai-ml | ai-ml/hugging-face-cli/SKILL.md | no_declared_dependency_metadata | c011e33ff672 | bundled / definition_only_not_runtime_wired |
| hugging-face-datasets | ai-ml | ai-ml/hugging-face-datasets/SKILL.md | no_declared_dependency_metadata | ca8fb141e351 | bundled / definition_only_not_runtime_wired |
| hugging-face-evaluation | ai-ml | ai-ml/hugging-face-evaluation/SKILL.md | dependency_metadata_present | a3acc51eb6df | bundled / definition_only_not_runtime_wired |
| hugging-face-model-trainer | ai-ml | ai-ml/hugging-face-model-trainer/SKILL.md | dependency_metadata_present | b1d3607420e1 | bundled / definition_only_not_runtime_wired |
| hugging-face-trackio | ai-ml | ai-ml/hugging-face-trackio/SKILL.md | no_declared_dependency_metadata | da2f366b8181 | bundled / definition_only_not_runtime_wired |
| imagen | ai-ml | ai-ml/imagen/SKILL.md | dependency_metadata_present | 3024eae8a603 | bundled / definition_only_not_runtime_wired |
| model-failover | ai-ml | ai-ml/model-failover/SKILL.md | no_declared_dependency_metadata | 92d446b756ab | bundled / definition_only_not_runtime_wired |
| multi-agent-patterns | ai-ml | ai-ml/multi-agent-patterns/SKILL.md | no_declared_dependency_metadata | c905d5c6f3ef | bundled / definition_only_not_runtime_wired |
| networkx | ai-ml | ai-ml/networkx/SKILL.md | dependency_metadata_present | 1aac65cf38ee | bundled / definition_only_not_runtime_wired |
| openai-image-gen | ai-ml | ai-ml/openai-image-gen/SKILL.md | no_declared_dependency_metadata | 366917d32d45 | bundled / definition_only_not_runtime_wired |
| openai-whisper | ai-ml | ai-ml/openai-whisper/SKILL.md | no_declared_dependency_metadata | 6211652a4ffe | bundled / definition_only_not_runtime_wired |
| openai-whisper-api | ai-ml | ai-ml/openai-whisper-api/SKILL.md | no_declared_dependency_metadata | c0ad50049a94 | bundled / definition_only_not_runtime_wired |
| pytorch-lightning | ai-ml | ai-ml/pytorch-lightning/SKILL.md | no_declared_dependency_metadata | b747b79c9fae | bundled / definition_only_not_runtime_wired |
| scikit-learn | ai-ml | ai-ml/scikit-learn/SKILL.md | dependency_metadata_present | b7de6b8d95a2 | bundled / definition_only_not_runtime_wired |
| statsmodels | ai-ml | ai-ml/statsmodels/SKILL.md | no_declared_dependency_metadata | da2754e05c3d | bundled / definition_only_not_runtime_wired |
| subagent-driven-development | ai-ml | ai-ml/subagent-driven-development/SKILL.md | no_declared_dependency_metadata | 994b89a5d294 | bundled / definition_only_not_runtime_wired |
| baidu-image-crawler | baidu-image-crawler | baidu-image-crawler/SKILL.md | dependency_metadata_present | 7647a95fcb23 | bundled / definition_only_not_runtime_wired |
| daily-review | daily-review | daily-review/SKILL.md | no_declared_dependency_metadata | 7b741abe3586 | bundled / definition_only_not_runtime_wired |
| csv-analyzer | data-analysis | data-analysis/csv-analyzer/SKILL.md | no_declared_dependency_metadata | 43cada14c097 | bundled / definition_only_not_runtime_wired |
| d3-viz | data-analysis | data-analysis/d3js-visualization/SKILL.md | no_declared_dependency_metadata | 4b1f820cd87c | bundled / definition_only_not_runtime_wired |
| deep-research | data-analysis | data-analysis/deep-research/SKILL.md | dependency_metadata_present | 80eb6d22b774 | bundled / definition_only_not_runtime_wired |
| implementing-search-filter | data-analysis | data-analysis/implementing-search-filter/SKILL.md | dependency_metadata_present | c3c09869c8b7 | bundled / definition_only_not_runtime_wired |
| matplotlib | data-analysis | data-analysis/matplotlib/SKILL.md | no_declared_dependency_metadata | 0c20cceb3a05 | bundled / definition_only_not_runtime_wired |
| postgres | data-analysis | data-analysis/sanjay-postgres/SKILL.md | dependency_metadata_present | d6d6b2c5c7ea | bundled / definition_only_not_runtime_wired |
| statistical-analysis | data-analysis | data-analysis/statistical-analysis/SKILL.md | no_declared_dependency_metadata | 5134736448ee | bundled / definition_only_not_runtime_wired |
| visualizing-data | data-analysis | data-analysis/visualizing-data/SKILL.md | dependency_metadata_present | 31cda9f7f770 | bundled / definition_only_not_runtime_wired |
| building-forms | design | design/building-forms/SKILL.md | no_declared_dependency_metadata | e7d715b1ade2 | bundled / definition_only_not_runtime_wired |
| algorand-vulnerability-scanner | design | design/building-secure-contracts/skills/algorand-vulnerability-scanner/SKILL.md | no_declared_dependency_metadata | 0ad3e96d8d58 | bundled / definition_only_not_runtime_wired |
| audit-prep-assistant | design | design/building-secure-contracts/skills/audit-prep-assistant/SKILL.md | dependency_metadata_present | ea7db549ff26 | bundled / definition_only_not_runtime_wired |
| cairo-vulnerability-scanner | design | design/building-secure-contracts/skills/cairo-vulnerability-scanner/SKILL.md | dependency_metadata_present | e2e64b00cfcf | bundled / definition_only_not_runtime_wired |
| code-maturity-assessor | design | design/building-secure-contracts/skills/code-maturity-assessor/SKILL.md | no_declared_dependency_metadata | 10e55d278593 | bundled / definition_only_not_runtime_wired |
| cosmos-vulnerability-scanner | design | design/building-secure-contracts/skills/cosmos-vulnerability-scanner/SKILL.md | no_declared_dependency_metadata | 0af2e52a6fe7 | bundled / definition_only_not_runtime_wired |
| guidelines-advisor | design | design/building-secure-contracts/skills/guidelines-advisor/SKILL.md | no_declared_dependency_metadata | 15b4864cd905 | bundled / definition_only_not_runtime_wired |
| secure-workflow-guide | design | design/building-secure-contracts/skills/secure-workflow-guide/SKILL.md | no_declared_dependency_metadata | d4f765622ae6 | bundled / definition_only_not_runtime_wired |
| solana-vulnerability-scanner | design | design/building-secure-contracts/skills/solana-vulnerability-scanner/SKILL.md | no_declared_dependency_metadata | 5e9040940ac0 | bundled / definition_only_not_runtime_wired |
| substrate-vulnerability-scanner | design | design/building-secure-contracts/skills/substrate-vulnerability-scanner/SKILL.md | no_declared_dependency_metadata | 611f69b41974 | bundled / definition_only_not_runtime_wired |
| token-integration-analyzer | design | design/building-secure-contracts/skills/token-integration-analyzer/SKILL.md | no_declared_dependency_metadata | f700683abe74 | bundled / definition_only_not_runtime_wired |
| ton-vulnerability-scanner | design | design/building-secure-contracts/skills/ton-vulnerability-scanner/SKILL.md | no_declared_dependency_metadata | 3a0fbdedb155 | bundled / definition_only_not_runtime_wired |
| building-tables | design | design/building-tables/SKILL.md | dependency_metadata_present | 793af3d451a2 | bundled / definition_only_not_runtime_wired |
| canvas | design | design/canvas/SKILL.md | no_declared_dependency_metadata | 931e65501872 | bundled / definition_only_not_runtime_wired |
| vercel-composition-patterns | design | design/composition-patterns/SKILL.md | no_declared_dependency_metadata | e38e0eaa6093 | bundled / definition_only_not_runtime_wired |
| creating-dashboards | design | design/creating-dashboards/SKILL.md | dependency_metadata_present | 3c150031250e | bundled / definition_only_not_runtime_wired |
| designing-apis | design | design/designing-apis/SKILL.md | no_declared_dependency_metadata | 8474e7e1d019 | bundled / definition_only_not_runtime_wired |
| designing-layouts | design | design/designing-layouts/SKILL.md | no_declared_dependency_metadata | 2bc1d2881961 | bundled / definition_only_not_runtime_wired |
| frontend-design | design | design/frontend-design/SKILL.md | no_declared_dependency_metadata | b81e2ff87ed8 | bundled / definition_only_not_runtime_wired |
| pg-aiguide | design | design/pg-aiguide/SKILL.md | no_declared_dependency_metadata | cb8720251958 | bundled / definition_only_not_runtime_wired |
| ppt-style-guide | design | design/ppt-style-guide/SKILL.md | dependency_metadata_present | 19a6bb09da34 | bundled / definition_only_not_runtime_wired |
| ui-ux-pro-max | design | design/ui-ux-pro-max/SKILL.md | dependency_metadata_present | b3ab45c714a7 | bundled / definition_only_not_runtime_wired |
| ux-ui-design-system | design | design/ux-ui-design-system/SKILL.md | no_declared_dependency_metadata | 0cb3d288d044 | bundled / definition_only_not_runtime_wired |
| web-design-guidelines | design | design/web-design-guidelines/SKILL.md | no_declared_dependency_metadata | f4647ca866a3 | bundled / definition_only_not_runtime_wired |
| coding-agent | development | development/coding-agent/SKILL.md | dependency_metadata_present | 98e98a9e1a09 | bundled / definition_only_not_runtime_wired |
| deploying-applications | development | development/deploying-applications/SKILL.md | dependency_metadata_present | 7b67605c4aed | bundled / definition_only_not_runtime_wired |
| differential-review | development | development/differential-review/skills/differential-review/SKILL.md | no_declared_dependency_metadata | 72a1e8dc98d5 | bundled / definition_only_not_runtime_wired |
| finishing-a-development-branch | development | development/finishing-a-development-branch/SKILL.md | no_declared_dependency_metadata | dd2f82c6dc85 | bundled / definition_only_not_runtime_wired |
| gh-issues | development | development/gh-issues/SKILL.md | no_declared_dependency_metadata | 9cfc67329014 | bundled / definition_only_not_runtime_wired |
| git-commit | development | development/git-commit/SKILL.md | no_declared_dependency_metadata | aea40e0a50ba | bundled / definition_only_not_runtime_wired |
| git-workflow-ci | development | development/git-workflow-management/SKILL.md | no_declared_dependency_metadata | 3f66bae5eb77 | bundled / definition_only_not_runtime_wired |
| github-pr-creation | development | development/github-pr-creation/SKILL.md | dependency_metadata_present | 749416050543 | bundled / definition_only_not_runtime_wired |
| github-pr-merge | development | development/github-pr-merge/SKILL.md | no_declared_dependency_metadata | 4e36449a8fff | bundled / definition_only_not_runtime_wired |
| github-pr-review | development | development/github-pr-review/SKILL.md | no_declared_dependency_metadata | 22f4e7c8559c | bundled / definition_only_not_runtime_wired |
| property-based-testing | development | development/property-based-testing/skills/property-based-testing/SKILL.md | no_declared_dependency_metadata | a027f88bb17a | bundled / definition_only_not_runtime_wired |
| receiving-code-review | development | development/receiving-code-review/SKILL.md | no_declared_dependency_metadata | c9382e92b8f3 | bundled / definition_only_not_runtime_wired |
| requesting-code-review | development | development/requesting-code-review/SKILL.md | no_declared_dependency_metadata | 2da31af22a58 | bundled / definition_only_not_runtime_wired |
| root-cause-tracing | development | development/root-cause-tracing/SKILL.md | no_declared_dependency_metadata | 4c3faa28453c | bundled / definition_only_not_runtime_wired |
| systematic-debugging | development | development/systematic-debugging/SKILL.md | no_declared_dependency_metadata | 4999cb851360 | bundled / definition_only_not_runtime_wired |
| tdd | development | development/tdd/SKILL.md | dependency_metadata_present | 0207117930d8 | bundled / definition_only_not_runtime_wired |
| test-driven-development | development | development/test-driven-development/SKILL.md | no_declared_dependency_metadata | 7dee67b4af6b | bundled / definition_only_not_runtime_wired |
| testing-strategies | development | development/testing-strategies/SKILL.md | no_declared_dependency_metadata | a3da25e3647d | bundled / definition_only_not_runtime_wired |
| verification-before-completion | development | development/verification-before-completion/SKILL.md | no_declared_dependency_metadata | ea52d15aabaf | bundled / definition_only_not_runtime_wired |
| writing-dockerfiles | development | development/writing-dockerfiles/SKILL.md | dependency_metadata_present | 588e76add5cb | bundled / definition_only_not_runtime_wired |
| writing-github-actions | development | development/writing-github-actions/SKILL.md | no_declared_dependency_metadata | af94116c2cbc | bundled / definition_only_not_runtime_wired |
| changelog-generator | documentation | documentation/changelog-generator/SKILL.md | no_declared_dependency_metadata | 819efc88f699 | bundled / definition_only_not_runtime_wired |
| diagram-generator | documentation | documentation/diagram-generator/SKILL.md | dependency_metadata_present | 96c2c0b24063 | bundled / definition_only_not_runtime_wired |
| markdown-to-epub-converter | documentation | documentation/markdown-to-epub/SKILL.md | no_declared_dependency_metadata | fa7ceaf36ab0 | bundled / definition_only_not_runtime_wired |
| writing-plans | documentation | documentation/writing-plans/SKILL.md | no_declared_dependency_metadata | 8a9198d4d9ef | bundled / definition_only_not_runtime_wired |
| writing-skills | documentation | documentation/writing-skills/SKILL.md | no_declared_dependency_metadata | d83a09d6a1c6 | bundled / definition_only_not_runtime_wired |
| fenglin-workflow | fenglin-workflow | fenglin-workflow/SKILL.md | no_declared_dependency_metadata | 7667c3bf0514 | bundled / definition_only_not_runtime_wired |
| playwright-skill | frameworks | frameworks/playwright-skill/SKILL.md | no_declared_dependency_metadata | f38b55f1a3f1 | bundled / definition_only_not_runtime_wired |
| vercel-react-best-practices | frameworks | frameworks/react-best-practices/SKILL.md | no_declared_dependency_metadata | b0bc1161fa20 | bundled / definition_only_not_runtime_wired |
| vercel-react-native-skills | frameworks | frameworks/react-native-skills/SKILL.md | no_declared_dependency_metadata | 4012750a5ccb | bundled / definition_only_not_runtime_wired |
| apple-notes | integrations | integrations/apple-notes/SKILL.md | dependency_metadata_present | c07631bb6ec0 | bundled / definition_only_not_runtime_wired |
| apple-reminders | integrations | integrations/apple-reminders/SKILL.md | dependency_metadata_present | 75f6b4bbe4eb | bundled / definition_only_not_runtime_wired |
| aws-agentic-ai | integrations | integrations/aws-agentic-ai/skills/aws-agentic-ai/SKILL.md | no_declared_dependency_metadata | 2ab9bb0da68e | bundled / definition_only_not_runtime_wired |
| aws-cdk-development | integrations | integrations/aws-cdk/skills/aws-cdk-development/SKILL.md | dependency_metadata_present | 9064f0fa50ca | bundled / definition_only_not_runtime_wired |
| aws-mcp-setup | integrations | integrations/aws-common/skills/aws-mcp-setup/SKILL.md | dependency_metadata_present | 2e7273ce47dc | bundled / definition_only_not_runtime_wired |
| aws-cost-operations | integrations | integrations/aws-cost-ops/skills/aws-cost-operations/SKILL.md | no_declared_dependency_metadata | 0df831407198 | bundled / definition_only_not_runtime_wired |
| bear-notes | integrations | integrations/bear-notes/SKILL.md | no_declared_dependency_metadata | 956251b2fca2 | bundled / definition_only_not_runtime_wired |
| agents-sdk | integrations | integrations/cloudflare-agents-sdk/SKILL.md | dependency_metadata_present | 6ee0d50d51db | bundled / definition_only_not_runtime_wired |
| durable-objects | integrations | integrations/cloudflare-durable-objects/SKILL.md | no_declared_dependency_metadata | 66053a19786c | bundled / definition_only_not_runtime_wired |
| wrangler | integrations | integrations/cloudflare-wrangler/SKILL.md | dependency_metadata_present | b1192e4c6701 | bundled / definition_only_not_runtime_wired |
| discord | integrations | integrations/discord/SKILL.md | no_declared_dependency_metadata | 06554ae01f77 | bundled / definition_only_not_runtime_wired |
| firebase-apk-scanner | integrations | integrations/firebase-apk-scanner/skills/firebase-apk-scanner/SKILL.md | no_declared_dependency_metadata | a1bc78a5e7e7 | bundled / definition_only_not_runtime_wired |
| gog | integrations | integrations/gog/SKILL.md | no_declared_dependency_metadata | ceb5ee5bb4f0 | bundled / definition_only_not_runtime_wired |
| google-calendar | integrations | integrations/google-calendar/SKILL.md | no_declared_dependency_metadata | afaad2a70228 | bundled / definition_only_not_runtime_wired |
| google-docs | integrations | integrations/google-docs/SKILL.md | no_declared_dependency_metadata | c0379b7f2fcf | bundled / definition_only_not_runtime_wired |
| google-drive | integrations | integrations/google-drive/SKILL.md | no_declared_dependency_metadata | 5db33f7f7af8 | bundled / definition_only_not_runtime_wired |
| google-sheets | integrations | integrations/google-sheets/SKILL.md | no_declared_dependency_metadata | 7221e7339f97 | bundled / definition_only_not_runtime_wired |
| himalaya | integrations | integrations/himalaya/SKILL.md | no_declared_dependency_metadata | afef5fe4e398 | bundled / definition_only_not_runtime_wired |
| imsg | integrations | integrations/imsg/SKILL.md | dependency_metadata_present | 55dd0913fbc1 | bundled / definition_only_not_runtime_wired |
| notion | integrations | integrations/notion/SKILL.md | no_declared_dependency_metadata | b1ec55de3a74 | bundled / definition_only_not_runtime_wired |
| obsidian | integrations | integrations/obsidian/SKILL.md | no_declared_dependency_metadata | b184f7079cf2 | bundled / definition_only_not_runtime_wired |
| obsidian-bases | integrations | integrations/obsidian-bases/SKILL.md | no_declared_dependency_metadata | 2b01f105aba1 | bundled / definition_only_not_runtime_wired |
| obsidian-markdown | integrations | integrations/obsidian-markdown/SKILL.md | no_declared_dependency_metadata | 50fd2c27aafa | bundled / definition_only_not_runtime_wired |
| managing-secrets | integrations | integrations/secret-management/SKILL.md | dependency_metadata_present | 4325ff6482ac | bundled / definition_only_not_runtime_wired |
| aws-serverless-eda | integrations | integrations/serverless-eda/skills/aws-serverless-eda/SKILL.md | no_declared_dependency_metadata | 3c063487fc3b | bundled / definition_only_not_runtime_wired |
| slack | integrations | integrations/slack/SKILL.md | no_declared_dependency_metadata | e3bf25727b1f | bundled / definition_only_not_runtime_wired |
| supabase-postgres-best-practices | integrations | integrations/supabase-postgres-best-practices/SKILL.md | no_declared_dependency_metadata | 8e5a86aa9299 | bundled / definition_only_not_runtime_wired |
| youtube-transcript | integrations | integrations/youtube-transcript/SKILL.md | dependency_metadata_present | 61c6ff19145b | bundled / definition_only_not_runtime_wired |
| 1password | others | others/1password/SKILL.md | no_declared_dependency_metadata | 9e9dace009cf | bundled / definition_only_not_runtime_wired |
| blogwatcher | others | others/blogwatcher/SKILL.md | no_declared_dependency_metadata | 33011ee6e0c3 | bundled / definition_only_not_runtime_wired |
| blucli | others | others/blucli/SKILL.md | no_declared_dependency_metadata | 38b96f404b6b | bundled / definition_only_not_runtime_wired |
| bluebubbles | others | others/bluebubbles/SKILL.md | no_declared_dependency_metadata | 41a9218692a5 | bundled / definition_only_not_runtime_wired |
| ceo-advisor | others | others/c-level-advisor/ceo-advisor/SKILL.md | no_declared_dependency_metadata | 9d3f78c6a917 | bundled / definition_only_not_runtime_wired |
| cto-advisor | others | others/c-level-advisor/cto-advisor/SKILL.md | no_declared_dependency_metadata | 4d093a539810 | bundled / definition_only_not_runtime_wired |
| camsnap | others | others/camsnap/SKILL.md | no_declared_dependency_metadata | 95039a10f662 | bundled / definition_only_not_runtime_wired |
| clawhub | others | others/clawhub/SKILL.md | no_declared_dependency_metadata | 2bae787841a3 | bundled / definition_only_not_runtime_wired |
| eightctl | others | others/eightctl/SKILL.md | dependency_metadata_present | a67bcb20983a | bundled / definition_only_not_runtime_wired |
| aws-solution-architect | others | others/engineering-team/aws-solution-architect/SKILL.md | no_declared_dependency_metadata | bae0f87a960a | bundled / definition_only_not_runtime_wired |
| code-reviewer | others | others/engineering-team/code-reviewer/SKILL.md | no_declared_dependency_metadata | 428a98e3e3bd | bundled / definition_only_not_runtime_wired |
| ms365-tenant-manager | others | others/engineering-team/ms365-tenant-manager/SKILL.md | no_declared_dependency_metadata | 2c76be07748f | bundled / definition_only_not_runtime_wired |
| senior-architect | others | others/engineering-team/senior-architect/SKILL.md | no_declared_dependency_metadata | 469ef270e575 | bundled / definition_only_not_runtime_wired |
| senior-backend | others | others/engineering-team/senior-backend/SKILL.md | no_declared_dependency_metadata | b1171953e867 | bundled / definition_only_not_runtime_wired |
| senior-computer-vision | others | others/engineering-team/senior-computer-vision/SKILL.md | no_declared_dependency_metadata | 6fb196ef397a | bundled / definition_only_not_runtime_wired |
| senior-data-engineer | others | others/engineering-team/senior-data-engineer/SKILL.md | dependency_metadata_present | 15dca8b0e5db | bundled / definition_only_not_runtime_wired |
| senior-data-scientist | others | others/engineering-team/senior-data-scientist/SKILL.md | no_declared_dependency_metadata | 1e6974ea6045 | bundled / definition_only_not_runtime_wired |
| senior-devops | others | others/engineering-team/senior-devops/SKILL.md | dependency_metadata_present | 478aed6e0ef1 | bundled / definition_only_not_runtime_wired |
| senior-frontend | others | others/engineering-team/senior-frontend/SKILL.md | dependency_metadata_present | 328da03c2918 | bundled / definition_only_not_runtime_wired |
| senior-fullstack | others | others/engineering-team/senior-fullstack/SKILL.md | dependency_metadata_present | 49626b216897 | bundled / definition_only_not_runtime_wired |
| senior-ml-engineer | others | others/engineering-team/senior-ml-engineer/SKILL.md | dependency_metadata_present | 9d37b6e6396c | bundled / definition_only_not_runtime_wired |
| senior-prompt-engineer | others | others/engineering-team/senior-prompt-engineer/SKILL.md | no_declared_dependency_metadata | ce3f1ef8aa55 | bundled / definition_only_not_runtime_wired |
| senior-qa | others | others/engineering-team/senior-qa/SKILL.md | no_declared_dependency_metadata | 186539ec7a1e | bundled / definition_only_not_runtime_wired |
| senior-secops | others | others/engineering-team/senior-secops/SKILL.md | no_declared_dependency_metadata | 4003717f9491 | bundled / definition_only_not_runtime_wired |
| senior-security | others | others/engineering-team/senior-security/SKILL.md | no_declared_dependency_metadata | 7c94d61bce07 | bundled / definition_only_not_runtime_wired |
| tdd-guide | others | others/engineering-team/tdd-guide/SKILL.md | no_declared_dependency_metadata | 313ee021abe3 | bundled / definition_only_not_runtime_wired |
| tech-stack-evaluator | others | others/engineering-team/tech-stack-evaluator/SKILL.md | no_declared_dependency_metadata | e87d3b2bfb27 | bundled / definition_only_not_runtime_wired |
| gifgrep | others | others/gifgrep/SKILL.md | no_declared_dependency_metadata | 3445f7eeed1b | bundled / definition_only_not_runtime_wired |
| goplaces | others | others/goplaces/SKILL.md | dependency_metadata_present | 36b7cca469b1 | bundled / definition_only_not_runtime_wired |
| healthcheck | others | others/healthcheck/SKILL.md | no_declared_dependency_metadata | adba53ffae39 | bundled / definition_only_not_runtime_wired |
| json-canvas | others | others/json-canvas/SKILL.md | no_declared_dependency_metadata | 174481b16ead | bundled / definition_only_not_runtime_wired |
| app-store-optimization | others | others/marketing-skill/app-store-optimization/SKILL.md | no_declared_dependency_metadata | ed379bddb746 | bundled / definition_only_not_runtime_wired |
| content-creator | others | others/marketing-skill/content-creator/SKILL.md | no_declared_dependency_metadata | 61ad2cd9ef57 | bundled / definition_only_not_runtime_wired |
| marketing-demand-acquisition | others | others/marketing-skill/marketing-demand-acquisition/SKILL.md | no_declared_dependency_metadata | 864582e41d7a | bundled / definition_only_not_runtime_wired |
| marketing-strategy-pmm | others | others/marketing-skill/marketing-strategy-pmm/SKILL.md | no_declared_dependency_metadata | b456c659e355 | bundled / definition_only_not_runtime_wired |
| social-media-analyzer | others | others/marketing-skill/social-media-analyzer/SKILL.md | no_declared_dependency_metadata | e3d4cfd78c2b | bundled / definition_only_not_runtime_wired |
| message-sending | others | others/message-sending/SKILL.md | no_declared_dependency_metadata | dcde1eb9b5a2 | bundled / definition_only_not_runtime_wired |
| model-usage | others | others/model-usage/SKILL.md | no_declared_dependency_metadata | dbdd6e603d27 | bundled / definition_only_not_runtime_wired |
| nano-banana-pro | others | others/nano-banana-pro/SKILL.md | no_declared_dependency_metadata | 9ab0838e959f | bundled / definition_only_not_runtime_wired |
| openhue | others | others/openhue/SKILL.md | no_declared_dependency_metadata | dae9ba55a656 | bundled / definition_only_not_runtime_wired |
| ordercli | others | others/ordercli/SKILL.md | no_declared_dependency_metadata | 14aef89ed6da | bundled / definition_only_not_runtime_wired |
| peekaboo | others | others/peekaboo/SKILL.md | no_declared_dependency_metadata | bb06f126ec6c | bundled / definition_only_not_runtime_wired |
| remotion-video | others | others/remotion-video/SKILL.md | no_declared_dependency_metadata | ae3125ca9c9d | bundled / definition_only_not_runtime_wired |
| sag | others | others/sag/SKILL.md | no_declared_dependency_metadata | d86b98ff6586 | bundled / definition_only_not_runtime_wired |
| sherpa-onnx-tts | others | others/sherpa-onnx-tts/SKILL.md | no_declared_dependency_metadata | 4ed0e7d7b771 | bundled / definition_only_not_runtime_wired |
| smart-search | others | others/smart-search/SKILL.md | no_declared_dependency_metadata | e812c54f930d | bundled / definition_only_not_runtime_wired |
| sonoscli | others | others/sonoscli/SKILL.md | no_declared_dependency_metadata | 0deb7507b233 | bundled / definition_only_not_runtime_wired |
| summarize | others | others/summarize/SKILL.md | no_declared_dependency_metadata | 3ec4a66d4f27 | bundled / definition_only_not_runtime_wired |
| using-git-worktrees | others | others/using-git-worktrees/SKILL.md | dependency_metadata_present | de9dcde34840 | bundled / definition_only_not_runtime_wired |
| using-superpowers | others | others/using-superpowers/SKILL.md | no_declared_dependency_metadata | 07d73726944e | bundled / definition_only_not_runtime_wired |
| web-asset-generator | others | others/web-asset-generator/SKILL.md | dependency_metadata_present | 97c0a124fb5a | bundled / definition_only_not_runtime_wired |
| brainstorming | pm-product | pm-product/brainstorming/SKILL.md | no_declared_dependency_metadata | 7a238df1ebf0 | bundled / definition_only_not_runtime_wired |
| executing-plans | pm-product | pm-product/executing-plans/SKILL.md | no_declared_dependency_metadata | d099fa42fd75 | bundled / definition_only_not_runtime_wired |
| planning-with-files | pm-product | pm-product/planning-with-files/SKILL.md | no_declared_dependency_metadata | d99ce66ad9af | bundled / definition_only_not_runtime_wired |
| agile-product-owner | pm-product | pm-product/product-team/agile-product-owner/SKILL.md | no_declared_dependency_metadata | 9ad964fe904d | bundled / definition_only_not_runtime_wired |
| product-manager-toolkit | pm-product | pm-product/product-team/product-manager-toolkit/SKILL.md | no_declared_dependency_metadata | 55940b94f44a | bundled / definition_only_not_runtime_wired |
| product-strategist | pm-product | pm-product/product-team/product-strategist/SKILL.md | no_declared_dependency_metadata | 6fb94d13b7d1 | bundled / definition_only_not_runtime_wired |
| ui-design-system | pm-product | pm-product/product-team/ui-design-system/SKILL.md | no_declared_dependency_metadata | 0ce37ac6b0f8 | bundled / definition_only_not_runtime_wired |
| ux-researcher-designer | pm-product | pm-product/product-team/ux-researcher-designer/SKILL.md | no_declared_dependency_metadata | 43a66a9c1683 | bundled / definition_only_not_runtime_wired |
| programming-workflow | programming-workflow | programming-workflow/SKILL.md | no_declared_dependency_metadata | 4b6102775d9c | bundled / definition_only_not_runtime_wired |
| project-standards | project-standards | project-standards/SKILL.md | no_declared_dependency_metadata | c67a82a2333b | bundled / definition_only_not_runtime_wired |
| constant-time-analysis | security-testing | security-testing/constant-time-analysis/skills/constant-time-analysis/SKILL.md | no_declared_dependency_metadata | 0ffc2357999f | bundled / definition_only_not_runtime_wired |
| insecure-defaults | security-testing | security-testing/insecure-defaults/skills/insecure-defaults/SKILL.md | no_declared_dependency_metadata | 6c6c54ab48dc | bundled / definition_only_not_runtime_wired |
| semgrep-rule-creator | security-testing | security-testing/semgrep-rule-creator/skills/semgrep-rule-creator/SKILL.md | no_declared_dependency_metadata | 1ae1e8d030f5 | bundled / definition_only_not_runtime_wired |
| designer | tool-development | tool-development/claude-designer-skill/SKILL.md | no_declared_dependency_metadata | e704f5e4d518 | bundled / definition_only_not_runtime_wired |
| context-fundamentals | tool-development | tool-development/context-fundamentals/SKILL.md | no_declared_dependency_metadata | 4f6b700280f6 | bundled / definition_only_not_runtime_wired |
| creating-skills | tool-development | tool-development/creating-skills/SKILL.md | no_declared_dependency_metadata | 9e5a4a38f3e7 | bundled / definition_only_not_runtime_wired |
| knowledge-base-maintenance | tool-development | tool-development/knowledge-base-maintenance/SKILL.md | no_declared_dependency_metadata | 12524b1b6738 | bundled / definition_only_not_runtime_wired |
| manus | tool-development | tool-development/manus/SKILL.md | no_declared_dependency_metadata | 7f6a151b402e | bundled / definition_only_not_runtime_wired |
| mcp-builder | tool-development | tool-development/mcp-builder/SKILL.md | no_declared_dependency_metadata | 0f4592dcb53c | bundled / definition_only_not_runtime_wired |
| mcporter | tool-development | tool-development/mcporter/SKILL.md | dependency_metadata_present | f4e9e568b20c | bundled / definition_only_not_runtime_wired |
| memory-systems | tool-development | tool-development/memory-systems/SKILL.md | no_declared_dependency_metadata | 96fa481e734a | bundled / definition_only_not_runtime_wired |
| notebooklm | tool-development | tool-development/notebooklm-skill/SKILL.md | dependency_metadata_present | 8fcfe1aa0452 | bundled / definition_only_not_runtime_wired |
| oracle | tool-development | tool-development/oracle/SKILL.md | no_declared_dependency_metadata | 903a053178a3 | bundled / definition_only_not_runtime_wired |
| session-logs | tool-development | tool-development/session-logs/SKILL.md | no_declared_dependency_metadata | 7c6a007031f8 | bundled / definition_only_not_runtime_wired |
| skill-creator | tool-development | tool-development/skill-creator/SKILL.md | no_declared_dependency_metadata | b2e3d83f6042 | bundled / definition_only_not_runtime_wired |
| skill-seekers | tool-development | tool-development/skill-seekers/SKILL.md | dependency_metadata_present | 1c08899e7c8c | bundled / definition_only_not_runtime_wired |
| skills-updater | tool-development | tool-development/skills-updater/SKILL.md | no_declared_dependency_metadata | 45a95902917d | bundled / definition_only_not_runtime_wired |
| tool-design | tool-development | tool-development/tool-design/SKILL.md | no_declared_dependency_metadata | 0f1d9661a832 | bundled / definition_only_not_runtime_wired |

## Verification evidence

Successful checks run after the implementation changes:

```text
python -m py_compile cli/aetherion_cli/skill_library.py cli/aetherion_cli/main.py cli/aetherion_cli/tui/gateway.py cli/aetherion_cli/tui/screens/skills.py
python -m pytest cli/tests/test_skill_library.py cli/tests/test_cli.py cli/tests/test_skill_creator.py cli/tests/test_tui_state.py -q
  143 passed
python -m pytest tests/integration/test_phase3_extensions.py -k "17a or 30 or 31a or 31_skill" -q
  4 passed, 43 deselected
```

The broader existing Phase 3 integration file was also run. Its failures are pre-existing/environmental or explicitly protected by repository policy: execution-boundary tests fail closed because agent execution is disabled, and REST tests cannot connect because PostgreSQL is not running. PostgreSQL was not started.

## Known gaps and boundaries

1. The local Skills Library is user/machine-scoped, while backend runtime skills are authenticated tenant-scoped. A secure attach/publish endpoint is still needed if local definitions should become backend runtime skills.
2. The CLI `runtime-*` commands preserve the pre-existing backend lifecycle surface. They are intentionally distinct from local `install`, `enable`, `disable`, `uninstall`, and `info` commands.
3. Existing catalogue definitions have heterogeneous prose and dependency references. The inventory records evidence but does not install dependencies or execute skill assets.
4. The repository's full integration run remains environment-limited by execution-disabled policy and unavailable PostgreSQL; those failures are reported, not claimed as passed.
5. The report contains the inventory evidence generated before the final implementation edits; rerun the scan if any packaged `SKILL.md` changes.
6. For backward compatibility, `skills create` without `--instructions` still delegates to the legacy conversation creator and `skills local` still exposes that legacy directory. Use `skills create --instructions ...` for the unified Skills Library record; a future migration command can reconcile legacy files explicitly.

## Recommended next step

Implement an explicit authenticated `skills attach`/`skills publish` flow that accepts a user-selected local definition, validates and hashes it server-side, stores it under the tenant owner, records approval/audit metadata, and supplies `selected_skill_ids` as a closed runtime allow-list. Do not make the backend read a machine-global local path automatically.

---
name: "find-skills"
description: "Helps users discover and install agent skills for the PAVP project. Activated when users ask 'how do I do X', 'is there a skill for X', or want to extend capabilities. Prioritizes Python/Streamlit/LiteLLM/Pydantic/LLM related skills."
---

# Find Skills (PAVP Project Edition)

This skill helps users discover and install skills from the open agent skills ecosystem, tailored for the PAVP project (Python + Streamlit + LiteLLM + Claude Code).

## When to Use This Skill

When the user:

- Asks "how do I do X" where X might be a common task with an existing skill
- Says "find a skill for X" or "is there a skill for X"
- Asks "can you do X" where X is a specialized capability
- Expresses interest in extending agent capabilities
- Wants to search for tools, templates, or workflows
- Mentions they wish they had help with a specific domain

### PAVP Project High-Frequency Domains

| Domain | Typical Queries | Example |
|---|---|---|
| **Python Development** | python, pydantic, asyncio, testing | `npx skills find python testing` |
| **Streamlit UI** | streamlit, ui, dashboard, frontend | `npx skills find streamlit ui` |
| **LLM Integration** | litellm, openai, llm, prompt-engineering | `npx skills find llm prompt` |
| **Code Quality** | review, lint, refactor, best-practices | `npx skills find python lint` |
| **DevOps** | docker, ci-cd, deployment | `npx skills find python deploy` |
| **Documentation** | docs, readme, api-docs | `npx skills find python docs` |

## What is the Skills CLI?

The Skills CLI (`npx skills`) is the package manager for the open agent skills ecosystem. Skills are modular packages that extend agent capabilities with specialized knowledge, workflows, and tools.

**Key commands:**

- `npx skills find [query]` - Search for skills interactively or by keyword
- `npx skills add <package>` - Install a skill from GitHub or other sources
- `npx skills check` - Check for skill updates
- `npx skills update` - Update all installed skills

**Browse skills at:** https://skills.sh/

## How to Help Users Find Skills

### Step 1: Understand What They Need

When a user asks for help with something, identify:

1. The domain (e.g., Python, Streamlit, LLM, testing)
2. The specific task (e.g., writing tests, UI design, prompt optimization)
3. Whether this is a common enough task that a skill likely exists

### Step 2: Check the Leaderboard First

Before running a CLI search, check the [skills.sh leaderboard](https://skills.sh/) to see if a well-known skill already exists for the domain.

PAVP-related popular skill sources:
- `anthropics/skills` - Frontend design, document processing (100K+ installs)
- `vercel-labs/agent-skills` - Web development best practices
- Python ecosystem skills typically come from community contributions

### Step 3: Search for Skills

If the leaderboard doesn't cover the user's need, run the find command:

```bash
npx skills find [query]
```

PAVP project search examples:

- User asks "how to optimize Streamlit performance?" -> `npx skills find streamlit performance`
- User asks "can you help write Pydantic models?" -> `npx skills find pydantic models`
- User asks "need LLM prompt optimization" -> `npx skills find prompt engineering`
- User asks "how to test FastAPI?" -> `npx skills find fastapi testing`

### Step 4: Verify Quality Before Recommending

**Do not recommend a skill based solely on search results.** Always verify:

1. **Install count** - Prefer skills with 1K+ installs. Be cautious with anything under 100.
2. **Source reputation** - Official sources (`anthropics`, `microsoft`) are more trustworthy than unknown authors.
3. **GitHub stars** - Check the source repository. A skill from a repo with <100 stars should be treated with skepticism.

### Step 5: Present Options to the User

When you find relevant skills, present them to the user with:

1. The skill name and what it does
2. The install count and source
3. The install command they can run
4. A link to learn more at skills.sh

Example response:

```
I found a skill that might help! "streamlit-best-practices" provides
Streamlit performance optimization and UI design guidelines.
(12K installs)

To install it:
npx skills add community/streamlit-skills@best-practices

Learn more: https://skills.sh/community/streamlit-skills/best-practices
```

### Step 6: Offer to Install

If the user wants to proceed, you can install the skill for them:

```bash
npx skills add <owner/repo@skill> -g -y
```

The `-g` flag installs globally (user-level) and `-y` skips confirmation prompts.

## PAVP Project Existing Skills

The `pavp_skill/` directory already contains these skills, no external search needed:

| Skill | Description | Trigger Condition |
|---|---|---|
| `overview` | PAVP project overview and context | Before any project task |
| `writing-plans` | Implementation plan and task decomposition | New features/major changes/saying "plan" |
| `find-skills` | Discover and install skills (this skill) | Looking for new capabilities |
| `streamlit-ui` | Streamlit UI design and debugging guidance | Modifying ui.py / Streamlit-related issues |
| `overview-aware-change` | Read overview before changes, update after completion | Project deletions, restructuring, logic changes |

## Search Tips

1. **Use specific keywords**: "streamlit cache" is better than just "cache"
2. **Try alternative terms**: If "prompt" doesn't work, try "llm" or "ai"
3. **Check popular sources**: Many Python skills come from `ComposioHQ/awesome-claude-skills`
4. **PAVP tech stack keywords**: python, streamlit, litellm, pydantic, fastapi, httpx, sqlite, claude-code

## When No Skills Are Found

If no relevant skills exist:

1. Acknowledge that no existing skill was found
2. Offer to help with the task directly using general capabilities
3. Suggest the user could create their own skill with `npx skills init`

Example:

```
I searched for skills related to "xyz" but didn't find any matches.
I can still help you with this task directly! Would you like me to proceed?

If this is something you do often, you could create your own skill:
npx skills init my-xyz-skill
```

# kt-defaults

Official out-of-the-box creatures and useful plugin pack for [KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium).

`kt-defaults` is the fastest way to understand how KohakuTerrarium is meant to be used in practice.
It is not just a demo folder and not only a reference package.
It gives you ready-to-run creatures you can use directly, inherit from, remix into your own packages, or place into terrariums when you want multi-agent composition.

## Why install it

With `kt-defaults`, you can:

- run useful creatures immediately
- start from strong default agent configs instead of building from zero
- inherit from official creatures and only override what you need
- reuse official terrariums if you want multi-agent setups
- get a practical plugin pack you can enable in your own configs

If KohakuTerrarium is the framework, `kt-defaults` is the official OOTB package that makes the framework immediately usable.

## Install

```bash
# Install from GitHub
kt install https://github.com/Kohaku-Lab/kt-defaults.git

# Or install the local copy in editable mode
kt install ./kt-defaults -e
```

After installation, use package paths like:

- `@kt-defaults/creatures/general`
- `@kt-defaults/creatures/swe`
- `@kt-defaults/creatures/reviewer`
- `@kt-defaults/terrariums/swe_team`

## Quick start

```bash
# Pick a model first
kt login codex
kt model default gpt-5.4

# Run useful creatures directly
kt run @kt-defaults/creatures/general
kt run @kt-defaults/creatures/swe
kt run @kt-defaults/creatures/reviewer
kt run @kt-defaults/creatures/researcher

# Optional: run a terrarium
kt terrarium run @kt-defaults/terrariums/swe_team
```

## What is included

### Creatures

These are the main reason most people install `kt-defaults`.

| Creature | What it is for | Base |
|----------|----------------|------|
| `general` | Broad default creature with the standard built-in tools and sub-agents | (none) |
| `swe` | Software engineering specialist for coding, repository work, debugging, and implementation | `general` |
| `reviewer` | Review-focused creature for code review, findings, and stricter evaluation | `general` |
| `ops` | Infrastructure and operations specialist | `general` |
| `researcher` | Research and analysis specialist | `general` |
| `creative` | Creative writing and ideation specialist | `general` |
| `root` | Root creature for operating terrariums through terrarium management tools | `general` |

### Terrariums

Terrariums are included as reusable compositions of those creatures.
They are useful, but secondary to the creature pack itself.

| Terrarium | What it is for | Creatures |
|-----------|----------------|-----------|
| `swe_team` | Software engineering team with root-agent coordination | `root`, `swe`, `reviewer` |
| `auto_research` | Automated experiment / iteration loop | specialized research workflow |
| `deep_research` | Multi-agent web research and synthesis | planner / researcher / synthesizer / critic |

### Plugins

`kt-defaults` also ships practical plugins you can turn on in your own creatures.

| Plugin | What it does |
|--------|---------------|
| `cost_tracker` | Track token usage and estimated cost per session |
| `event_logger` | Write structured JSONL logs of agent activity |
| `multimodal_guard` | Guard or constrain multimodal usage |
| `rag_reader` | Add RAG-style reading support |
| `seamless_memory` | Improve memory continuity and retrieval behavior |

## Recommended starting points

### I want a general-purpose creature

```bash
kt run @kt-defaults/creatures/general
```

Start here if you want the broad default experience.

### I want a coding creature

```bash
kt run @kt-defaults/creatures/swe
```

Start here if your main use case is repository work, implementation, debugging, or coding assistance.

### I want a stricter review creature

```bash
kt run @kt-defaults/creatures/reviewer
```

Use this when you want a more review-oriented posture than `general` or `swe`.

### I want research or analysis

```bash
kt run @kt-defaults/creatures/researcher
```

### I want operations / infra help

```bash
kt run @kt-defaults/creatures/ops
```

### I want creative drafting or ideation

```bash
kt run @kt-defaults/creatures/creative
```

## The intended workflow

A common workflow with `kt-defaults` looks like this:

### 1. Use a creature directly

```bash
kt run @kt-defaults/creatures/swe
```

### 2. Inherit from it instead of rebuilding from scratch

```yaml
name: my_team_coder
base_config: "@kt-defaults/creatures/swe"

controller:
  llm: claude-sonnet-4.6

system_prompt_file: prompts/system.md
```

### 3. Add your own modules or plugins

```yaml
tools:
  - name: my_tool
    type: custom
    module: ./custom/my_tool.py
    class: MyTool

plugins:
  - name: cost_tracker
    type: package
    module: kt_defaults.plugins.cost_tracker
    class: CostTrackerPlugin
```

### 4. Optionally compose creatures into a terrarium

```bash
kt terrarium run @kt-defaults/terrariums/swe_team
```

That is the real role of `kt-defaults`: not just to provide examples, but to provide a usable base ecosystem.

## Using the plugins

Enable packaged plugins in your creature config:

```yaml
plugins:
  - name: cost_tracker
    type: package
    module: kt_defaults.plugins.cost_tracker
    class: CostTrackerPlugin
    options:
      budget_usd: 5.0
      warn_at: 0.8

  - name: event_logger
    type: package
    module: kt_defaults.plugins.event_logger
    class: EventLoggerPlugin
    options:
      path: ./logs/events.jsonl
```

You can use these directly in your own creatures, whether or not those creatures inherit from `kt-defaults`.

## Package structure

```text
kt-defaults/
  creatures/      # Official reusable creature configs
  terrariums/     # Reusable terrarium configs built on those creatures
  kt_defaults/    # Python package for plugins, tools, triggers, I/O
  kohaku.yaml     # Package manifest
```

Cross-package references use `@package-name/path` syntax:

```yaml
base_config: "@kt-defaults/creatures/swe"
```

## Build your own package on top

You can treat `kt-defaults` as a base ecosystem and publish your own package on top of it.

Typical pattern:

- inherit from `@kt-defaults/creatures/general` or `@kt-defaults/creatures/swe`
- add your own prompts, tools, and plugins
- publish your own package
- install it with `kt install`

That way, users can consume your creatures the same way they consume the official ones.

## See also

- [Root README](../README.md)
- [Getting Started](../docs/guides/getting-started.md)
- [Creatures Guide](../docs/guides/creatures.md)
- [Plugins Guide](../docs/guides/plugins.md)
- [Examples](../examples/README.md)

## License

KohakuTerrarium License 1.0.
See [LICENSE](LICENSE).

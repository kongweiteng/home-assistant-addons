# Research sources and evidence contract

## Source families

Use these public sources without authentication:

- HASSbian discovery leads:
  - <https://bbs.hassbian.com/forum.php?mod=guide&view=newthread>
  - <https://bbs.hassbian.com/forum.php?mod=guide&view=hot>
  - <https://bbs.hassbian.com/archiver/>
- Home Assistant official integration directory: <https://www.home-assistant.io/integrations/>
- Home Assistant official Add-on documentation: <https://www.home-assistant.io/addons/>
- Home Assistant Core source: <https://github.com/home-assistant/core>
- Home Assistant official Add-ons source: <https://github.com/home-assistant/addons>
- HACS documentation: <https://hacs.xyz/docs/use/repositories/type/integration/>
- HACS public integration registry data: <https://data-v2.hacs.xyz/integration/data.json>
- Candidate's original GitHub repository and its Releases, manifest, repository metadata, issues, and installation documentation.

HASSbian, search result pages, mirrors, reposts, AI summaries, and social posts are non-authoritative. A HASSbian lead must be confirmed by at least one official, HACS, or original GitHub source before it can appear as a candidate.

## Candidate classification

| Kind | Install method | Minimum authoritative evidence |
| --- | --- | --- |
| `official_integration` | `config_flow` | Home Assistant official integration page; Core source is preferred as second evidence |
| `addon` | `supervisor` | Original Add-on repository or official Add-on page, plus version/config evidence |
| `hacs` | `hacs` | Original GitHub repository plus HACS documentation or metadata evidence; only registry-confirmed candidates can reach `recommend` |
| `manual_custom_component` | `manual` | Original GitHub repository; always grade at most `review` in P1 |

## Compatibility grades

- `verified`: an explicit current Home Assistant compatibility statement, official inclusion, or manifest/version evidence was checked.
- `likely`: the repository is active and metadata looks compatible, but no explicit current-version statement was found.
- `unknown`: evidence is missing, ambiguous, stale, or only community discussion was found.
- `incompatible`: the source explicitly conflicts with the current Home Assistant version or required architecture.

Never turn `likely` into `verified` based only on recent commits, popularity, stars, or a forum report.

## Normalizer input

Write a UTF-8 JSON document with this shape:

```json
{
  "query": "家庭能源统计",
  "candidates": [
    {
      "name": "Example Integration",
      "kind": "official_integration",
      "source_url": "https://www.home-assistant.io/integrations/example/",
      "maintainer": "Home Assistant Core",
      "latest_release": "2026.7.0",
      "last_activity_at": "2026-07-20T00:00:00Z",
      "compatibility": "verified",
      "compatibility_note": "Present in the current official integration directory.",
      "required_permissions": ["network"],
      "install_method": "config_flow",
      "risk_summary": "Requires an external account and user-completed OAuth.",
      "evidence": [
        {
          "source_type": "official",
          "url": "https://www.home-assistant.io/integrations/example/",
          "note": "Official integration documentation"
        },
        {
          "source_type": "github",
          "url": "https://github.com/home-assistant/core/tree/dev/homeassistant/components/example",
          "note": "Current component source and manifest"
        }
      ]
    }
  ]
}
```

The normalizer accepts one to three inputs, validates public HTTPS links, rejects HASSbian-only evidence, calculates maintenance status, and computes the recommendation grade. Do not override its grade in the final answer.

## Risk review

Always mention relevant risks such as:

- OAuth, QR code, captcha, account, or device-selection steps that require the user;
- cloud dependency, polling load, local-network access, host networking, privileged access, device mounts, or broad filesystem access;
- Core restart, YAML changes, Recorder growth, database migrations, or irreversible configuration changes;
- unsupported architecture, abandoned maintenance, missing releases, unclear license, or manual file installation;
- access to cameras, locks, alarm systems, people, location, credentials, or other sensitive domains.

Research output must not claim that a candidate is installed, tested, or accepted in the user's live Home Assistant.

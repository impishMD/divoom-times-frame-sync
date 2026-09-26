# Security policy

**English** | [Русский](/docs/ru/SECURITY.md)

## Reporting a vulnerability

Please report vulnerabilities privately. If the repository offers **Security → Report a vulnerability**, use GitHub's private vulnerability reporting form. Availability depends on whether private reporting has been enabled for the repository.

If that option is unavailable and you do not have a private contact for the maintainer, open an issue asking for a private reporting channel. Do not include vulnerability details, exploit steps, credentials, or personal data in that public request.

In the private report, include the affected version or commit, the relevant provider or frame operation, the impact, and steps to reproduce using a separate album with synthetic data. Redact sensitive information from logs before sharing them.

## Handling sensitive data

A public album link may grant access to photos without a password. Do not publish sharing links, frame tokens, `.env`, `sources.toml`, `data/`, network captures, or device databases in issues or pull requests.

Divoom Times Frame Sync reads source albums and changes frame albums through the local device API. The sync journal records which items the service manages, allowing it to distinguish those items from manually added content. Keep this metadata in persistent storage with restricted access.

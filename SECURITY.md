# Security policy

## Reporting

Please do not open a public issue containing credentials, private transcripts, audio, or network access details. Contact the repository owner privately through GitHub.

## Local deployment rules

- Keep `config.env` outside version control.
- Use a randomly generated `PA_TOKEN` and rotate it after exposure.
- Restrict port 8787 to the phone/Pi network with a firewall.
- Treat transcript text as potentially sensitive when selecting an external AI cleaner.
- Do not commit `archive/`, `out/`, `logs/`, queues, receipts, `.venv/`, or Tasker exports containing tokens.
